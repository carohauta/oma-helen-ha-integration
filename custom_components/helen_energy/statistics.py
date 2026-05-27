"""Statistics manager for Helen Energy integration."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helenservice import RESOLUTION_HOUR, RESOLUTION_QUARTER, HelenApiClient
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

from .const import DOMAIN, STATISTICS_BACKFILL_HOURS
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
        """Detect missing hours in `series` and import statistics for them.

        Shared by import_recent_statistics and backfill_statistics: queries
        existing statistics over the data window, finds gaps, builds cumulative
        statistics for the missing hours, and imports the three streams.

        Args:
            series: Hourly measurement series from the API
        """
        if not series:
            _LOGGER.warning("No interval data to process")
            return

        # Get existing statistics covering the full API data range
        now_utc = datetime.now(ZoneInfo("UTC"))
        earliest_api_timestamp = min(
            datetime.fromisoformat(entry.start).astimezone(ZoneInfo("UTC"))
            for entry in series
        )
        existing_consumption = await self._get_existing_statistics_in_window(
            self.consumption_statistic_id, earliest_api_timestamp, now_utc
        )
        _LOGGER.debug(
            "Found %d existing consumption records in window",
            len(existing_consumption),
        )

        # Detect gaps (missing timestamps)
        gap_series = self._detect_gaps(series, existing_consumption)
        if not gap_series:
            _LOGGER.debug("No gaps detected, all data already imported")
            return

        _LOGGER.info(
            "Detected %d missing hourly intervals, filling gaps", len(gap_series)
        )

        # Build statistics for gaps only
        consumption_stats, cost_stats, fixed_cost_stats = (
            await self._build_statistics_for_gaps(
                gap_series,
                self.consumption_statistic_id,
                self.cost_statistic_id,
                self.fixed_cost_statistic_id,
            )
        )

        if not consumption_stats:
            _LOGGER.debug("No valid gap data to import (missing electricity or prices)")
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
            None,  # unit_class None for currency; may need revisiting in HA 2026.11
            cost_stats,
        )
        if fixed_cost_stats:
            await self._import_statistics(
                self.fixed_cost_statistic_id,
                f"{self.config_entry_title} - Fixed Prices",
                "EUR",
                None,
                fixed_cost_stats,
            )

        _LOGGER.info(
            "Successfully filled %d gaps for %s (fixed_cost=%s)",
            len(consumption_stats),
            self.entity_id,
            "yes" if fixed_cost_stats else "no",
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
        """Fetch 15-minute interval data from API and aggregate to hourly.

        Uses STATISTICS_BACKFILL_HOURS constant to determine how far back to fetch.
        Fetches 15-minute data for precise pricing, then aggregates to hourly.

        Returns:
            List of measurement series with hourly intervals
        """
        # Calculate date range
        end_date = date.today()
        start_date = end_date - timedelta(days=STATISTICS_BACKFILL_HOURS // 24 + 1)

        _LOGGER.debug(
            "Fetching 15-minute interval data from %s to %s", start_date, end_date
        )

        try:
            # Fetch 15-minute data using executor to avoid blocking
            response: MeasurementsWithSpotPriceResponse = (
                await self.hass.async_add_executor_job(
                    self.api_client.get_measurements_with_spot_prices,
                    start_date,
                    end_date,
                    RESOLUTION_QUARTER,  # 15-minute intervals for precise pricing
                )
            )

            _LOGGER.debug(
                "Received %d 15-minute intervals from API (resolution: %s)",
                len(response.series),
                response.resolution,
            )

            if response.missing_series:
                _LOGGER.warning(
                    "API reported %d missing 15-minute intervals", len(response.missing_series)
                )

            # Aggregate 15-minute intervals to hourly
            hourly_series = self._aggregate_to_hourly(response.series)
            _LOGGER.debug(
                "Aggregated to %d hourly intervals", len(hourly_series)
            )

            return hourly_series

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

    def _aggregate_to_hourly(
        self, quarter_series: list[MeasurementsWithSpotPriceSeries]
    ) -> list[MeasurementsWithSpotPriceSeries]:
        """Aggregate 15-minute intervals to hourly intervals.

        Args:
            quarter_series: List of 15-minute measurement intervals

        Returns:
            List of hourly measurement intervals with summed consumption and averaged prices
        """
        # Group intervals by hour
        hourly_data = defaultdict(list)

        for entry in quarter_series:
            try:
                # Parse timestamp and normalize to UTC for consistent hour_key
                entry_time = datetime.fromisoformat(entry.start)
                # Convert to UTC to ensure consistent timezone across all runs
                entry_time_utc = entry_time.astimezone(ZoneInfo("UTC"))
                # Round down to the hour and strip microseconds for consistent keys
                hour_start = entry_time_utc.replace(minute=0, second=0, microsecond=0)
                hour_key = hour_start.isoformat()

                hourly_data[hour_key].append(entry)
            except Exception as err:
                _LOGGER.warning(
                    "Failed to parse timestamp %s during aggregation: %s", entry.start, err
                )
                continue

        # Create hourly aggregated entries
        hourly_series = []
        for hour_key in sorted(hourly_data.keys()):
            quarters = hourly_data[hour_key]

            # Skip if we don't have exactly 4 quarters (incomplete or duplicate data)
            if len(quarters) != 4:
                if len(quarters) > 4:
                    _LOGGER.warning(
                        "Skipping hour %s with duplicate data (%d/4 quarters) - possible API issue",
                        hour_key,
                        len(quarters),
                    )
                else:
                    _LOGGER.debug(
                        "Skipping incomplete hour %s (only %d/4 quarters)",
                        hour_key,
                        len(quarters),
                    )
                continue

            # Sum electricity consumption for the hour. A genuine zero-consumption
            # hour must be preserved (not treated as missing) so it is imported as a
            # zero-delta point rather than left as a permanent gap.
            #
            # IMPORTANT: Only aggregate if ALL quarters have data. Partial data
            # (e.g., 3/4 quarters) creates incorrect totals and should be treated
            # as pending/unavailable (return None).
            electricity_values = [q.electricity for q in quarters]
            if all(e is not None for e in electricity_values):
                # All quarters have data - sum them
                hourly_electricity = round(sum(electricity_values), 2)
            else:
                # Some quarters are null - hour is incomplete
                hourly_electricity = None

            # Average spot prices for the hour (already includes VAT)
            # IMPORTANT: Only average if ALL quarters have price data.
            # Partial pricing data would create inaccurate hourly averages.
            price_values = [q.electricity_spot_prices_vat for q in quarters]
            if all(p is not None for p in price_values):
                # All quarters have prices - average them
                hourly_price = round(sum(price_values) / len(price_values), 4)
            else:
                # Some quarters lack prices - hour is incomplete
                hourly_price = None

            # Create hourly entry
            # Note: We're reusing the MeasurementsWithSpotPriceSeries structure
            # but with aggregated hourly values
            hourly_entry = MeasurementsWithSpotPriceSeries(
                start=hour_key,
                stop=quarters[-1].stop,  # End of the last quarter
                electricity=hourly_electricity,
                electricity_spot_prices_vat=hourly_price,
            )

            hourly_series.append(hourly_entry)

        return hourly_series

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

    def _detect_gaps(
        self,
        api_series: list[MeasurementsWithSpotPriceSeries],
        existing_timestamps: dict[datetime, float],
    ) -> list[MeasurementsWithSpotPriceSeries]:
        """Find API data for timestamps missing in existing statistics.

        Only includes entries that have actual electricity data (not future/pending data).

        Args:
            api_series: Hourly data from API
            existing_timestamps: Dict of existing timestamps and cumulative values

        Returns:
            List of API series entries for missing timestamps that have data
        """
        missing_series = []
        pending_count = 0

        for entry in api_series:
            # Parse and normalize timestamp
            entry_time = datetime.fromisoformat(entry.start)
            entry_time_utc = entry_time.astimezone(ZoneInfo("UTC"))
            entry_time_utc = entry_time_utc.replace(minute=0, second=0, microsecond=0)

            # Check if timestamp is missing
            if entry_time_utc not in existing_timestamps:
                # Only count as a gap if we have actual electricity data
                electricity = self._extract_electricity_value(entry)
                if electricity is not None:
                    missing_series.append(entry)
                else:
                    pending_count += 1

        if missing_series:
            _LOGGER.debug(
                "Detected %d fillable gaps out of %d API records (%d pending without data)",
                len(missing_series),
                len(api_series),
                pending_count,
            )
        elif pending_count > 0:
            _LOGGER.debug(
                "No fillable gaps, %d recent hours pending electricity data",
                pending_count,
            )

        return missing_series

    async def _get_cumulative_at_or_before_timestamp(
        self, statistic_id: str, timestamp: datetime
    ) -> tuple[float, datetime | None]:
        """Get cumulative value at or before a specific timestamp.

        Queries for the last statistic <= timestamp.

        Args:
            statistic_id: The statistic ID to query
            timestamp: The timestamp to query before (UTC)

        Returns:
            Tuple of (cumulative_sum, timestamp) - sum and timestamp of last record before/at timestamp
        """
        try:
            # Query from epoch to timestamp
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC")),  # Start from epoch
                timestamp,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )

            if statistic_id in stats and stats[statistic_id]:
                # Get the last record (chronologically latest before/at timestamp)
                last_stat = stats[statistic_id][-1]

                # Handle both Unix timestamp and datetime
                timestamp_raw = last_stat["start"]
                if isinstance(timestamp_raw, datetime):
                    last_timestamp = timestamp_raw
                else:
                    last_timestamp = datetime.fromtimestamp(
                        timestamp_raw, tz=ZoneInfo("UTC")
                    )

                return last_stat.get("sum", 0.0), last_timestamp

            # No records before this timestamp
            return 0.0, None

        except Exception as err:
            _LOGGER.warning(
                "Error querying cumulative before %s for %s: %s",
                timestamp.isoformat(),
                statistic_id,
                err,
            )
            return 0.0, None

    async def _build_statistics_for_gaps(
        self,
        gap_series: list[MeasurementsWithSpotPriceSeries],
        consumption_statistic_id: str,
        cost_statistic_id: str,
        fixed_cost_statistic_id: str | None = None,
    ) -> tuple[list[StatisticData], list[StatisticData], list[StatisticData]]:
        """Build statistics for gap filling with correct cumulative values.

        For each gap entry, query the cumulative value from the record
        immediately before it, then build forward.

        Args:
            gap_series: List of missing hourly measurements from API
            consumption_statistic_id: ID for consumption statistics
            cost_statistic_id: ID for cost statistics
            fixed_cost_statistic_id: ID for fixed cost statistics (optional)

        Returns:
            Tuple of (consumption_statistics, cost_statistics, fixed_cost_statistics)
        """
        consumption_stats = []
        cost_stats = []
        fixed_cost_stats = []

        # Sort by timestamp to process in chronological order
        sorted_series = sorted(gap_series, key=lambda x: x.start)

        # Track cumulative values as we build statistics
        # Initialize to None to detect when we need to query the database
        last_cumulative_consumption = None
        last_cumulative_cost = None
        last_cumulative_fixed_cost = None
        last_timestamp = None

        # Check if we should calculate fixed cost statistics
        has_fixed_price = self._fixed_unit_price is not None

        for entry in sorted_series:
            # Parse and normalize timestamp
            utc_time = self._convert_to_utc(entry.start)
            utc_time = utc_time.replace(minute=0, second=0, microsecond=0)

            # Filter out future data
            now_utc = datetime.now(ZoneInfo("UTC"))
            if utc_time > now_utc:
                continue

            # Check if this gap is consecutive with the previous one
            is_consecutive = (
                last_timestamp is not None
                and utc_time == last_timestamp + timedelta(hours=1)
            )

            if is_consecutive:
                # Use the cumulative from the previous gap we just filled
                cumulative_consumption = last_cumulative_consumption
                cumulative_cost = last_cumulative_cost
                cumulative_fixed_cost = last_cumulative_fixed_cost
            else:
                # Non-consecutive gap or first gap - query the database
                cumulative_consumption, _ = await self._get_cumulative_at_or_before_timestamp(
                    consumption_statistic_id, utc_time
                )
                cumulative_cost, _ = await self._get_cumulative_at_or_before_timestamp(
                    cost_statistic_id, utc_time
                )
                if has_fixed_price and fixed_cost_statistic_id:
                    cumulative_fixed_cost, _ = await self._get_cumulative_at_or_before_timestamp(
                        fixed_cost_statistic_id, utc_time
                    )
                else:
                    cumulative_fixed_cost = 0.0

            # Extract values
            electricity = self._extract_electricity_value(entry)
            spot_price = self._extract_spot_price_value(entry)

            if electricity is None:
                _LOGGER.debug(
                    "Skipping gap at %s: missing electricity data", utc_time.isoformat()
                )
                continue

            if spot_price is None:
                _LOGGER.debug(
                    "Skipping gap at %s: missing spot price data", utc_time.isoformat()
                )
                continue

            # Calculate hourly costs
            hourly_cost = electricity * spot_price
            hourly_fixed_cost = (
                electricity * (self._fixed_unit_price / 100.0)
                if has_fixed_price
                else 0.0
            )

            # Add to cumulative
            cumulative_consumption += electricity
            cumulative_cost += hourly_cost
            cumulative_fixed_cost += hourly_fixed_cost

            # Create consumption statistics
            consumption_stats.append(
                StatisticData(
                    start=utc_time,
                    state=safe_round(cumulative_consumption),
                    sum=safe_round(cumulative_consumption),
                )
            )

            # Create spot cost statistics
            cost_stats.append(
                StatisticData(
                    start=utc_time,
                    state=safe_round(cumulative_cost),
                    sum=safe_round(cumulative_cost),
                )
            )

            # Create fixed cost statistics (only if we have a fixed price)
            if has_fixed_price:
                fixed_cost_stats.append(
                    StatisticData(
                        start=utc_time,
                        state=safe_round(cumulative_fixed_cost),
                        sum=safe_round(cumulative_fixed_cost),
                    )
                )

            _LOGGER.debug(
                "Gap fill at %s: electricity=%.3f kWh, spot_cost=%.2f EUR, fixed_cost=%.2f EUR",
                utc_time.isoformat(),
                electricity,
                cumulative_cost,
                cumulative_fixed_cost if has_fixed_price else 0.0,
            )

            # Update tracking variables for next iteration
            last_cumulative_consumption = cumulative_consumption
            last_cumulative_cost = cumulative_cost
            last_cumulative_fixed_cost = cumulative_fixed_cost
            last_timestamp = utc_time

        return consumption_stats, cost_stats, fixed_cost_stats

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
