"""Statistics manager for Helen Energy integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helenservice import RESOLUTION_HOUR, HelenApiClient
from helenservice.api_exceptions import InvalidApiResponseException
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
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .const import DOMAIN, STATISTICS_BACKFILL_HOURS, STATISTICS_MAX_GAP_WAIT_HOURS
from .utils import safe_round

_LOGGER = logging.getLogger(__name__)


class HelenStatisticsManager:
    """Manage statistics import for Helen Energy consumption data."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: HelenApiClient,
        entity_id: str,
        config_entry_id: str,
        config_entry_title: str,
        fixed_unit_price: float | None = None,
    ) -> None:
        """Initialize the statistics manager.

        Args:
            hass: Home Assistant instance
            api_client: Helen API client instance
            entity_id: Entity ID of the consumption sensor
            config_entry_id: Config entry ID (used to create unique statistics IDs)
            config_entry_title: User-friendly config entry title for display names
            fixed_unit_price: Fixed unit price in cents/kWh (for fixed-price contracts)
        """
        self.hass = hass
        self.api_client = api_client
        self.entity_id = entity_id
        self.config_entry_title = config_entry_title
        self._fixed_unit_price = fixed_unit_price

        # Create unique statistic_ids for each config entry to prevent collisions
        # Remove hyphens and lowercase (statistic_ids only allow lowercase, digits, underscores)
        # Take first 8 chars for a short, unique suffix
        suffix = config_entry_id.replace("-", "").lower()[:8]
        self.consumption_statistic_id = f"{DOMAIN}:hourly_energy_consumption_{suffix}"
        self.cost_statistic_id = f"{DOMAIN}:hourly_cost_spot_{suffix}"
        self.fixed_cost_statistic_id = f"{DOMAIN}:hourly_cost_fixed_{suffix}"

        _LOGGER.debug(
            "Initialized HelenStatisticsManager for %s with statistic_ids: %s (consumption), %s (spot cost), %s (fixed cost) (%d hour backfill, fixed_price=%s)",
            entity_id,
            self.consumption_statistic_id,
            self.cost_statistic_id,
            self.fixed_cost_statistic_id,
            STATISTICS_BACKFILL_HOURS,
            f"{fixed_unit_price} cents/kWh" if fixed_unit_price else "None",
        )

    def set_fixed_unit_price(self, price: float | None) -> None:
        """Set the fixed unit price (cents/kWh) used for fixed-cost statistics."""
        self._fixed_unit_price = price

    async def import_recent_statistics(self) -> None:
        """Import recent hourly statistics with gap detection and filling."""
        _LOGGER.debug("Starting statistics import for %s", self.entity_id)

        try:
            # Fetch 15-minute interval data, aggregate to hourly, fill any gaps
            series = await self._fetch_interval_data()
            await self._fill_gaps(series)
        except Exception as err:
            _LOGGER.error(
                "Error importing statistics for %s: %s",
                self.entity_id,
                err,
                exc_info=True,
            )
            raise

    async def _fill_gaps(
        self, series: list[MeasurementsWithSpotPriceSeries]
    ) -> None:
        """Extend the cumulative sum chain consecutively from the last DB record.

        Walks hour-by-hour from `last_db_hour + 1h` to the latest API hour. For each
        missing hour: stops if recent (waits for Helen to backfill), or zero-fills if
        older than STATISTICS_MAX_GAP_WAIT_HOURS to unblock the chain. This keeps the
        sum strictly monotonic and rules out the negative-spike class of bugs.
        """
        if not series:
            _LOGGER.warning("No interval data to process")
            return

        now_utc = datetime.now(ZoneInfo("UTC")).replace(
            minute=0, second=0, microsecond=0
        )

        api_entries: dict[datetime, MeasurementsWithSpotPriceSeries] = {}
        for entry in series:
            hour = self._convert_to_utc(entry.start).replace(
                minute=0, second=0, microsecond=0
            )
            api_entries[hour] = entry

        if not api_entries:
            _LOGGER.warning("No usable API entries to process")
            return

        earliest_api = min(api_entries.keys())
        latest_api = max(api_entries.keys())

        window_end = now_utc + timedelta(hours=1)
        existing_consumption = await self._get_existing_statistics_in_window(
            self.consumption_statistic_id, earliest_api, window_end
        )
        existing_cost = await self._get_existing_statistics_in_window(
            self.cost_statistic_id, earliest_api, window_end
        )
        has_fixed_price = self._fixed_unit_price is not None
        existing_fixed_cost: dict[datetime, float] = {}
        if has_fixed_price and self.fixed_cost_statistic_id:
            existing_fixed_cost = await self._get_existing_statistics_in_window(
                self.fixed_cost_statistic_id, earliest_api, window_end
            )

        if existing_consumption:
            last_db_hour = max(existing_consumption.keys())
            cumulative_consumption = existing_consumption[last_db_hour]
            cumulative_cost = existing_cost.get(last_db_hour, 0.0)
            cumulative_fixed_cost = existing_fixed_cost.get(last_db_hour, 0.0)
            walk_start = last_db_hour + timedelta(hours=1)
        else:
            cumulative_consumption = 0.0
            cumulative_cost = 0.0
            cumulative_fixed_cost = 0.0
            walk_start = earliest_api

        if walk_start > latest_api:
            _LOGGER.debug(
                "Nothing to write: DB already up to %s, latest API hour is %s",
                walk_start.isoformat(),
                latest_api.isoformat(),
            )
            return

        consumption_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        fixed_cost_stats: list[StatisticData] = []

        zero_filled = 0
        current_hour = walk_start
        while current_hour <= latest_api:
            entry = api_entries.get(current_hour)
            electricity = self._extract_electricity_value(entry) if entry else None
            spot_price = self._extract_spot_price_value(entry) if entry else None

            if electricity is None or spot_price is None:
                age_hours = (now_utc - current_hour).total_seconds() / 3600
                if age_hours > STATISTICS_MAX_GAP_WAIT_HOURS:
                    electricity = 0.0
                    spot_price = 0.0
                    zero_filled += 1
                    _LOGGER.warning(
                        "Zero-filling %s for %s: no data after %.0fh wait",
                        current_hour.isoformat(),
                        self.entity_id,
                        age_hours,
                    )
                else:
                    _LOGGER.debug(
                        "Stopping at %s: missing data (age=%.1fh, wait up to %dh)",
                        current_hour.isoformat(),
                        age_hours,
                        STATISTICS_MAX_GAP_WAIT_HOURS,
                    )
                    break

            hourly_cost = electricity * spot_price
            hourly_fixed_cost = (
                electricity * (self._fixed_unit_price / 100.0)
                if has_fixed_price
                else 0.0
            )

            cumulative_consumption += electricity
            cumulative_cost += hourly_cost
            cumulative_fixed_cost += hourly_fixed_cost

            consumption_stats.append(
                StatisticData(
                    start=current_hour,
                    state=safe_round(cumulative_consumption),
                    sum=safe_round(cumulative_consumption),
                )
            )
            cost_stats.append(
                StatisticData(
                    start=current_hour,
                    state=safe_round(cumulative_cost),
                    sum=safe_round(cumulative_cost),
                )
            )
            if has_fixed_price:
                fixed_cost_stats.append(
                    StatisticData(
                        start=current_hour,
                        state=safe_round(cumulative_fixed_cost),
                        sum=safe_round(cumulative_fixed_cost),
                    )
                )

            _LOGGER.debug(
                "Wrote %s: electricity=%.3f kWh, cumulative=%.2f kWh, spot_cost=%.2f EUR, fixed_cost=%.2f EUR",
                current_hour.isoformat(),
                electricity,
                cumulative_consumption,
                cumulative_cost,
                cumulative_fixed_cost if has_fixed_price else 0.0,
            )

            current_hour += timedelta(hours=1)

        if not consumption_stats:
            _LOGGER.debug("No new statistics to import")
            return

        await self._import_statistics(
            self.consumption_statistic_id,
            f"{self.config_entry_title} - Consumption",
            UnitOfEnergy.KILO_WATT_HOUR,
            "energy",
            consumption_stats,
        )
        await self._import_statistics(
            self.cost_statistic_id,
            f"{self.config_entry_title} - Spot Prices",
            "EUR",
            None,
            cost_stats,
        )
        if has_fixed_price and fixed_cost_stats:
            await self._import_statistics(
                self.fixed_cost_statistic_id,
                f"{self.config_entry_title} - Fixed Prices",
                "EUR",
                None,
                fixed_cost_stats,
            )

        _LOGGER.info(
            "Wrote %d hour(s) for %s (fixed_cost=%s, zero_filled=%d)",
            len(consumption_stats),
            self.entity_id,
            "yes" if has_fixed_price and fixed_cost_stats else "no",
            zero_filled,
        )

    async def backfill_statistics(
        self, start_date: date, end_date: date
    ) -> None:
        """Backfill statistics for a custom date range.

        Uses hourly API resolution (not quarter) for larger date ranges.
        Only fills gaps - skips hours that already have statistics.

        Args:
            start_date: First date to backfill (inclusive)
            end_date: Last date to backfill (inclusive)
        """
        _LOGGER.info(
            "Starting backfill for %s: %s to %s (%d days)",
            self.entity_id,
            start_date,
            end_date,
            (end_date - start_date).days,
        )

        contract_start = await self.hass.async_add_executor_job(
            self.api_client.get_contract_start_date
        )

        # Only prevent fetch if BOTH dates are before contract start
        if contract_start is not None and contract_start > end_date:
            _LOGGER.warning(
                "Backfill period (%s to %s) is entirely before contract start %s - no data available",
                start_date,
                end_date,
                contract_start,
            )
            return

        # API will handle partial overlap (contract started during the range)

        try:
            # Fetch hourly data from API
            response: MeasurementsWithSpotPriceResponse = (
                await self.hass.async_add_executor_job(
                    self.api_client.get_measurements_with_spot_prices,
                    start_date,
                    end_date,
                    RESOLUTION_HOUR,  # Use hourly resolution for large ranges
                )
            )

            _LOGGER.debug(
                "Received %d hourly intervals from API (resolution: %s)",
                len(response.series),
                response.resolution,
            )

            if not response.series:
                _LOGGER.warning("No data received from API for date range")
                return

            if response.missing_series:
                _LOGGER.warning(
                    "API reported %d missing hourly intervals in requested range",
                    len(response.missing_series),
                )

            await self._fill_gaps(response.series)

        except InvalidApiResponseException as err:
            # Check if this is a "no relevant contract" error
            error_msg = str(err)
            if "no-relevant-contract" in error_msg.lower() or "no relevant contracts" in error_msg.lower():
                _LOGGER.warning(
                    "Backfill failed for %s: Requested date range (%s to %s) is outside contract period",
                    self.entity_id,
                    start_date,
                    end_date,
                )
                # Re-raise with more context to be caught by service handler
                raise ValueError(
                    f"Requested date range ({start_date} to {end_date}) is outside your contract period. "
                    "Please select a date range within your active contract dates."
                ) from err
            else:
                # Other API errors - log and re-raise
                _LOGGER.error(
                    "Helen API error during backfill for %s: %s",
                    self.entity_id,
                    err,
                    exc_info=True,
                )
                raise
        except Exception as err:
            # Other unexpected errors
            _LOGGER.error(
                "Error during backfill for %s: %s",
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
        Fetches hourly data directly from Helen's API (same as manual backfill).

        Returns:
            List of measurement series with hourly intervals
        """
        # Calculate date range
        end_date = date.today()
        start_date = end_date - timedelta(days=STATISTICS_BACKFILL_HOURS // 24 + 1)

        # Clamp to contract start so new users (<72h old contract) get partial data
        # instead of a 403 for the pre-contract portion of the window
        try:
            contract_start = await self.hass.async_add_executor_job(
                self.api_client.get_contract_start_date
            )

            # Only prevent fetch if BOTH dates are before contract start
            if contract_start is not None and contract_start > end_date:
                _LOGGER.debug(
                    "Fetch window (%s to %s) is entirely before contract start %s - skipping",
                    start_date,
                    end_date,
                    contract_start,
                )
                return

            # API handles partial overlap
        except Exception as err:
            _LOGGER.debug("Could not get contract start date, using default window: %s", err)

        _LOGGER.debug(
            "Fetching hourly interval data from %s to %s", start_date, end_date
        )

        try:
            # Fetch hourly data using executor to avoid blocking
            response: MeasurementsWithSpotPriceResponse = (
                await self.hass.async_add_executor_job(
                    self.api_client.get_measurements_with_spot_prices,
                    start_date,
                    end_date,
                    RESOLUTION_HOUR,  # Hourly intervals (same as manual backfill)
                )
            )

            _LOGGER.debug(
                "Received %d hourly intervals from API (resolution: %s)",
                len(response.series),
                response.resolution,
            )

            if response.missing_series:
                _LOGGER.warning(
                    "API reported %d missing hourly intervals", len(response.missing_series)
                )

            # Return hourly data directly (no aggregation needed)
            return response.series

        except InvalidApiResponseException as err:
            error_msg = str(err)
            if "no-relevant-contract" in error_msg.lower() or "no relevant contracts" in error_msg.lower():
                _LOGGER.warning(
                    "Cannot fetch interval data for %s: Date range outside contract period",
                    self.entity_id,
                )
                # Return empty series rather than crashing - coordinator will handle gracefully
                return []
            else:
                _LOGGER.error(
                    "Helen API error fetching interval data for %s: %s",
                    self.entity_id,
                    err,
                    exc_info=True,
                )
                raise
        except Exception as err:
            _LOGGER.error(
                "Error fetching interval data for %s: %s",
                self.entity_id,
                err,
                exc_info=True,
            )
            raise

    async def _get_existing_statistics_in_window(
        self, statistic_id: str, start_time: datetime, end_time: datetime
    ) -> dict[datetime, float]:
        """Get all existing statistics in the time window.

        Args:
            statistic_id: The statistic ID to query
            start_time: Start of window (UTC)
            end_time: End of window (UTC)

        Returns:
            Dict mapping timestamp to cumulative sum value
        """
        try:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                end_time,
                {statistic_id},
                "hour",
                None,  # units
                {"sum"},  # types
            )

            existing_data = {}
            if statistic_id in stats:
                for stat in stats[statistic_id]:
                    # Handle both Unix timestamp (float) and datetime objects
                    timestamp_raw = stat["start"]
                    if isinstance(timestamp_raw, datetime):
                        # Ensure timezone-aware UTC
                        if timestamp_raw.tzinfo is None:
                            timestamp = timestamp_raw.replace(tzinfo=ZoneInfo("UTC"))
                        else:
                            timestamp = timestamp_raw.astimezone(ZoneInfo("UTC"))
                    else:
                        timestamp = datetime.fromtimestamp(
                            timestamp_raw, tz=ZoneInfo("UTC")
                        )

                    # Normalize to remove microseconds
                    timestamp = timestamp.replace(minute=0, second=0, microsecond=0)
                    existing_data[timestamp] = stat.get("sum", 0.0)

            _LOGGER.debug(
                "Found %d existing records for %s in window %s to %s",
                len(existing_data),
                statistic_id,
                start_time.isoformat(),
                end_time.isoformat(),
            )

            return existing_data

        except Exception as err:
            _LOGGER.warning(
                "Error querying existing statistics for %s: %s",
                statistic_id,
                err,
            )
            return {}

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

    async def _import_statistics(
        self,
        statistic_id: str,
        name: str,
        unit_of_measurement: str,
        unit_class: str | None,
        statistics: list[StatisticData],
    ) -> None:
        """Import a cumulative statistics stream into the HA database.

        Args:
            statistic_id: External statistic ID
            name: Human-readable statistic name
            unit_of_measurement: Unit (e.g. kWh or EUR)
            unit_class: HA unit class, or None for currency
            statistics: List of StatisticData to import
        """
        metadata_kwargs = {
            "has_sum": True,
            "name": name,
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": unit_of_measurement,
            "unit_class": unit_class,
        }

        if HAS_MEAN_TYPE:
            metadata_kwargs["mean_type"] = StatisticMeanType.NONE
        else:
            metadata_kwargs["has_mean"] = False

        metadata = StatisticMetaData(**metadata_kwargs)
        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug("Statistics imported successfully for %s", statistic_id)
