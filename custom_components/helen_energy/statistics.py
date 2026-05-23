"""Statistics manager for Helen Energy integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helenservice import RESOLUTION_HOUR, HelenApiClient
from helenservice.api_response import (
    MeasurementsWithSpotPriceResponse,
    MeasurementsWithSpotPriceSeries,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

# Import StatisticMeanType if available (HA 2026.11+)
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    HAS_MEAN_TYPE = True
except ImportError:
    HAS_MEAN_TYPE = False
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

        # Create statistic_ids for all three statistics
        self.consumption_statistic_id = f"{DOMAIN}:monthly_consumption"
        self.price_statistic_id = f"{DOMAIN}:spot_price"
        self.cost_statistic_id = f"{DOMAIN}:monthly_cost"

        _LOGGER.debug(
            "Initialized HelenStatisticsManager for %s with statistic_ids: %s (consumption), %s (price), %s (cost) (%d hour backfill)",
            entity_id,
            self.consumption_statistic_id,
            self.price_statistic_id,
            self.cost_statistic_id,
            STATISTICS_BACKFILL_HOURS,
        )

    async def import_recent_statistics(self) -> None:
        """Import recent hourly statistics (consumption, price, cost) into HA database."""
        _LOGGER.debug("Starting statistics import for %s", self.entity_id)

        try:
            # Fetch hourly interval data for the backfill period
            series = await self._fetch_interval_data()

            if not series:
                _LOGGER.warning("No interval data received from API")
                return

            # Get last cumulative totals for consumption and cost
            last_consumption, last_cost = await self._get_last_cumulative_totals()
            _LOGGER.debug(
                "Last cumulative: consumption=%.2f kWh, cost=%.2f EUR",
                last_consumption,
                last_cost,
            )

            # Build statistics data from intervals
            consumption_stats, price_stats, cost_stats = self._build_statistics_from_intervals(
                series, last_consumption, last_cost
            )

            if not consumption_stats:
                _LOGGER.debug("No new statistics to import")
                return

            # Import all three statistics types
            await self._import_consumption_statistics(consumption_stats)
            await self._import_price_statistics(price_stats)
            await self._import_cost_statistics(cost_stats)

            _LOGGER.info(
                "Successfully imported %d consumption, %d price, and %d cost statistics entries for %s",
                len(consumption_stats),
                len(price_stats),
                len(cost_stats),
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
        """Fetch hourly interval data from API.

        Uses STATISTICS_BACKFILL_HOURS constant to determine how far back to fetch.

        Returns:
            List of measurement series with hourly intervals
        """
        # Calculate date range
        end_date = date.today()
        start_date = end_date - timedelta(days=STATISTICS_BACKFILL_HOURS // 24 + 1)

        _LOGGER.debug(
            "Fetching hourly interval data from %s to %s", start_date, end_date
        )

        # Fetch data using executor to avoid blocking
        response: MeasurementsWithSpotPriceResponse = (
            await self.hass.async_add_executor_job(
                self.api_client.get_measurements_with_spot_prices,
                start_date,
                end_date,
                RESOLUTION_HOUR,  # Hourly intervals
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

    async def _get_last_cumulative_totals(self) -> tuple[float, float]:
        """Get the last cumulative totals for both consumption and cost statistics.

        Returns:
            Tuple of (last_consumption, last_cost) in kWh and EUR
        """
        # Get last consumption cumulative
        last_consumption = await self._get_last_cumulative_total(
            self.consumption_statistic_id
        )

        # Get last cost cumulative
        last_cost = await self._get_last_cumulative_total(
            self.cost_statistic_id
        )

        return last_consumption, last_cost

    async def _get_last_cumulative_total(self, statistic_id: str) -> float:
        """Get the last cumulative total from existing statistics.

        Args:
            statistic_id: The statistic ID to query

        Returns:
            Last cumulative sum, or 0.0 if no statistics exist
        """
        try:
            last_stats = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,  # number of stats to retrieve
                statistic_id,  # statistic_id
                True,  # convert_units
                {"sum"},  # types to retrieve
            )

            if statistic_id in last_stats and last_stats[statistic_id]:
                last_sum = last_stats[statistic_id][0].get("sum", 0.0)
                return safe_round(last_sum)

            _LOGGER.debug(
                "No existing statistics found for %s, starting from 0.0", statistic_id
            )
            return 0.0

        except Exception as err:
            _LOGGER.warning(
                "Error querying last statistics for %s, starting from 0.0: %s",
                statistic_id,
                err,
            )
            return 0.0

    def _build_statistics_from_intervals(
        self,
        series: list[MeasurementsWithSpotPriceSeries],
        last_consumption_cumulative: float,
        last_cost_cumulative: float,
    ) -> tuple[list[StatisticData], list[StatisticData], list[StatisticData]]:
        """Build statistics from hourly interval data.

        Args:
            series: List of hourly measurement intervals from API
            last_consumption_cumulative: Last known cumulative consumption (kWh)
            last_cost_cumulative: Last known cumulative cost (EUR)

        Returns:
            Tuple of (consumption_statistics, price_statistics, cost_statistics)
        """
        consumption_statistics = []
        price_statistics = []
        cost_statistics = []
        cumulative_consumption = last_consumption_cumulative
        cumulative_cost = last_cost_cumulative
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

            # Verify timestamp is at top of hour (should already be from API)
            if utc_time.minute != 0 or utc_time.second != 0:
                _LOGGER.warning(
                    "Received non-hourly timestamp from API: %s", utc_time
                )
                continue

            # Filter out future data (API can return predictions)
            if utc_time > now_utc:
                future_count += 1
                continue

            # Extract electricity consumption (kWh)
            electricity = self._extract_electricity_value(entry)
            if electricity is None:
                missing_count += 1
                continue

            # Extract spot price (EUR/kWh including VAT)
            spot_price = self._extract_spot_price_value(entry)
            if spot_price is None:
                _LOGGER.warning(
                    "Missing spot price for timestamp %s, skipping price/cost statistics",
                    utc_time
                )
                missing_count += 1
                continue

            # Calculate hourly cost (kWh * EUR/kWh = EUR)
            hourly_cost = electricity * spot_price

            # Add to cumulative totals
            cumulative_consumption += electricity
            cumulative_cost += hourly_cost

            # Create consumption statistics data point (cumulative)
            consumption_statistics.append(
                StatisticData(
                    start=utc_time,
                    state=safe_round(cumulative_consumption),
                    sum=safe_round(cumulative_consumption),
                )
            )

            # Create price statistics data point (non-cumulative, state only)
            price_statistics.append(
                StatisticData(
                    start=utc_time,
                    state=safe_round(spot_price, 4),  # 4 decimals for price precision
                )
            )

            # Create cost statistics data point (cumulative)
            cost_statistics.append(
                StatisticData(
                    start=utc_time,
                    state=safe_round(cumulative_cost),
                    sum=safe_round(cumulative_cost),
                )
            )

        # Log filtering results
        if future_count > 0:
            _LOGGER.debug("Filtered out %d future intervals", future_count)
        if missing_count > 0:
            _LOGGER.debug("Skipped %d intervals with missing data", missing_count)

        _LOGGER.debug(
            "Built %d hourly statistics entries (consumption: %.2f kWh, cost: %.2f EUR)",
            len(consumption_statistics),
            cumulative_consumption,
            cumulative_cost,
        )

        return consumption_statistics, price_statistics, cost_statistics

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

    def _extract_spot_price_value(
        self, entry: MeasurementsWithSpotPriceSeries
    ) -> float | None:
        """Extract spot price value from measurement entry.

        Args:
            entry: Measurement series entry

        Returns:
            Spot price in EUR/kWh including VAT, or None if missing
        """
        # API provides price in cents/kWh, convert to EUR/kWh
        if entry.electricity_spot_prices_vat is None:
            return None

        return entry.electricity_spot_prices_vat / 100.0

    async def _import_consumption_statistics(self, statistics: list[StatisticData]) -> None:
        """Import consumption statistics into Home Assistant database.

        Args:
            statistics: List of StatisticData to import
        """
        metadata_kwargs = {
            "has_sum": True,
            "name": "Helen Energy Hourly Statistics",
            "source": DOMAIN,
            "statistic_id": self.consumption_statistic_id,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "unit_class": "energy",
        }

        if HAS_MEAN_TYPE:
            metadata_kwargs["mean_type"] = StatisticMeanType.NONE
        else:
            metadata_kwargs["has_mean"] = False

        metadata = StatisticMetaData(**metadata_kwargs)
        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug(
            "Consumption statistics imported successfully for %s",
            self.consumption_statistic_id,
        )

    async def _import_price_statistics(self, statistics: list[StatisticData]) -> None:
        """Import price statistics into Home Assistant database.

        Args:
            statistics: List of StatisticData to import
        """
        metadata_kwargs = {
            "has_sum": False,  # Price is non-cumulative
            "name": "Helen Energy Spot Price",
            "source": DOMAIN,
            "statistic_id": self.price_statistic_id,
            "unit_of_measurement": "EUR/kWh",
        }

        if HAS_MEAN_TYPE:
            metadata_kwargs["mean_type"] = StatisticMeanType.NONE
        else:
            metadata_kwargs["has_mean"] = False

        metadata = StatisticMetaData(**metadata_kwargs)
        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug(
            "Price statistics imported successfully for %s", self.price_statistic_id
        )

    async def _import_cost_statistics(self, statistics: list[StatisticData]) -> None:
        """Import cost statistics into Home Assistant database.

        Args:
            statistics: List of StatisticData to import
        """
        metadata_kwargs = {
            "has_sum": True,
            "name": "Helen Energy Hourly Cost",
            "source": DOMAIN,
            "statistic_id": self.cost_statistic_id,
            "unit_of_measurement": "EUR",
            "unit_class": "monetary",
        }

        if HAS_MEAN_TYPE:
            metadata_kwargs["mean_type"] = StatisticMeanType.NONE
        else:
            metadata_kwargs["has_mean"] = False

        metadata = StatisticMetaData(**metadata_kwargs)
        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug(
            "Cost statistics imported successfully for %s", self.cost_statistic_id
        )
