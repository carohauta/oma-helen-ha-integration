"""Statistics manager for Helen Energy integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helenservice import RESOLUTION_QUARTER, HelenApiClient
from helenservice.api_response import (
    MeasurementsWithSpotPriceResponse,
    MeasurementsWithSpotPriceSeries,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STATISTICS_BACKFILL_HOURS

_LOGGER = logging.getLogger(__name__)


def safe_round(value: float | None, decimals: int = 2) -> float:
    """Safely round a value, returning 0.0 if value is None or non-numeric."""
    if value is None:
        return 0.0
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return 0.0


class HelenStatisticsManager:
    """Manage statistics import for Helen Energy consumption data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: HelenApiClient,
        entity_id: str,
    ) -> None:
        """Initialize the statistics manager.

        Args:
            hass: Home Assistant instance
            api_client: Helen API client instance
            entity_id: Entity ID of the consumption sensor
        """
        self.hass = hass
        self.api_client = api_client
        self.entity_id = entity_id

        # Create statistic_id in domain:object_id format for external statistics
        self.statistic_id = f"{DOMAIN}:monthly_consumption"

        _LOGGER.debug(
            "Initialized HelenStatisticsManager for %s with statistic_id %s (%d hour backfill)",
            entity_id,
            self.statistic_id,
            STATISTICS_BACKFILL_HOURS,
        )

    async def import_recent_statistics(self) -> None:
        """Import recent 15-minute interval statistics into HA database."""
        _LOGGER.debug("Starting statistics import for %s", self.entity_id)

        try:
            # Fetch 15-minute interval data for the backfill period
            series = await self._fetch_interval_data()

            if not series:
                _LOGGER.warning("No interval data received from API")
                return

            # Get last cumulative total from existing statistics
            last_cumulative = await self._get_last_cumulative_total()
            _LOGGER.debug("Last cumulative total: %.2f kWh", last_cumulative)

            # Build statistics data from intervals
            statistics = self._build_statistics_from_intervals(series, last_cumulative)

            if not statistics:
                _LOGGER.debug("No new statistics to import")
                return

            # Import statistics into HA database
            await self._import_statistics(statistics)

            _LOGGER.info(
                "Successfully imported %d statistics entries for %s",
                len(statistics),
                self.entity_id,
            )

        except Exception as err:
            _LOGGER.error(
                "Error importing statistics for %s: %s",
                self.entity_id,
                err,
                exc_info=True,
            )
            raise

    async def _fetch_interval_data(
        self,
    ) -> list[MeasurementsWithSpotPriceSeries]:
        """Fetch 15-minute interval data from API.

        Uses STATISTICS_BACKFILL_HOURS constant to determine how far back to fetch.

        Returns:
            List of measurement series with 15-minute intervals
        """
        # Calculate date range
        end_date = date.today()
        start_date = end_date - timedelta(days=STATISTICS_BACKFILL_HOURS // 24 + 1)

        _LOGGER.debug(
            "Fetching 15-minute interval data from %s to %s", start_date, end_date
        )

        # Fetch data using executor to avoid blocking
        response: MeasurementsWithSpotPriceResponse = (
            await self.hass.async_add_executor_job(
                self.api_client.get_measurements_with_spot_prices,
                start_date,
                end_date,
                RESOLUTION_QUARTER,  # 15-minute intervals
            )
        )

        _LOGGER.debug(
            "Received %d intervals from API (resolution: %s)",
            len(response.series),
            response.resolution,
        )

        if response.missing_series:
            _LOGGER.warning(
                "API reported %d missing intervals", len(response.missing_series)
            )

        return response.series

    async def _get_last_cumulative_total(self) -> float:
        """Get the last cumulative total from existing statistics.

        Returns:
            Last cumulative sum, or 0.0 if no statistics exist
        """
        try:
            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,  # number of stats to retrieve
                self.statistic_id,  # statistic_id
                True,  # convert_units
                {"sum"},  # types to retrieve
            )

            if self.statistic_id in last_stats and last_stats[self.statistic_id]:
                last_sum = last_stats[self.statistic_id][0].get("sum", 0.0)
                return safe_round(last_sum)

            _LOGGER.debug("No existing statistics found, starting from 0.0")
            return 0.0

        except Exception as err:
            _LOGGER.warning(
                "Error querying last statistics, starting from 0.0: %s", err
            )
            return 0.0

    def _build_statistics_from_intervals(
        self,
        series: list[MeasurementsWithSpotPriceSeries],
        last_cumulative: float,
    ) -> list[StatisticData]:
        """Build cumulative statistics from interval data.

        Args:
            series: List of measurement intervals from API
            last_cumulative: Last known cumulative total

        Returns:
            List of StatisticData objects ready for import
        """
        statistics = []
        cumulative = last_cumulative
        now_utc = datetime.now(ZoneInfo("UTC"))
        future_count = 0
        missing_count = 0

        for entry in series:
            # Parse timestamp and convert to UTC
            try:
                utc_time = self._convert_to_utc(entry.start)
            except Exception as err:
                _LOGGER.warning("Failed to parse timestamp %s: %s", entry.start, err)
                continue

            # Filter out future data (API can return predictions)
            if utc_time > now_utc:
                future_count += 1
                continue

            # Extract electricity value (with fallback to electricity_transfer)
            electricity = self._extract_electricity_value(entry)

            # Skip missing data
            if electricity is None:
                missing_count += 1
                continue

            # Add interval to cumulative total
            cumulative += electricity

            # Create statistics data point
            statistics.append(
                StatisticData(
                    start=utc_time,
                    state=safe_round(cumulative),
                    sum=safe_round(cumulative),
                )
            )

        # Log filtering results
        if future_count > 0:
            _LOGGER.debug("Filtered out %d future intervals", future_count)
        if missing_count > 0:
            _LOGGER.debug("Skipped %d intervals with missing data", missing_count)

        _LOGGER.debug(
            "Built %d statistics entries (cumulative: %.2f kWh)",
            len(statistics),
            cumulative,
        )

        return statistics

    def _convert_to_utc(self, helsinki_timestamp: str) -> datetime:
        """Convert Helsinki timezone timestamp to UTC.

        Args:
            helsinki_timestamp: ISO 8601 timestamp string in Helsinki timezone

        Returns:
            Datetime in UTC timezone
        """
        # Parse ISO 8601 timestamp (includes timezone info)
        helsinki_dt = datetime.fromisoformat(helsinki_timestamp)

        # Convert to UTC
        utc_dt = helsinki_dt.astimezone(ZoneInfo("UTC"))

        return utc_dt

    def _extract_electricity_value(
        self, entry: MeasurementsWithSpotPriceSeries
    ) -> float | None:
        """Extract electricity value from measurement entry.

        Args:
            entry: Measurement series entry

        Returns:
            Electricity value in kWh, or None if missing
        """
        return entry.electricity

    async def _import_statistics(self, statistics: list[StatisticData]) -> None:
        """Import statistics into Home Assistant database.

        Args:
            statistics: List of StatisticData to import
        """
        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="Helen Energy Monthly Consumption",
            source=DOMAIN,
            statistic_id=self.statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug("Statistics imported successfully for %s", self.statistic_id)
