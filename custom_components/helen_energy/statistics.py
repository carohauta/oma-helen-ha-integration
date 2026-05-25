"""Statistics manager for Helen Energy integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from helenservice import RESOLUTION_HOUR, RESOLUTION_QUARTER, HelenApiClient
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
    statistics_during_period,
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
        fixed_unit_price: float | None = None,
    ) -> None:
        """Initialize the statistics manager.

        Args:
            hass: Home Assistant instance
            api_client: Helen API client instance
            entity_id: Entity ID of the consumption sensor
            fixed_unit_price: Fixed unit price in cents/kWh (for fixed-price contracts)
        """
        self.hass = hass
        self.api_client = api_client
        self.entity_id = entity_id
        self._fixed_unit_price = fixed_unit_price

        # Create statistic_ids for consumption and cost (for Energy Dashboard)
        self.consumption_statistic_id = f"{DOMAIN}:hourly_energy_consumption"
        self.cost_statistic_id = f"{DOMAIN}:hourly_cost_spot"
        self.fixed_cost_statistic_id = f"{DOMAIN}:hourly_cost_fixed"

        _LOGGER.debug(
            "Initialized HelenStatisticsManager for %s with statistic_ids: %s (consumption), %s (spot cost), %s (fixed cost) (%d hour backfill, fixed_price=%s)",
            entity_id,
            self.consumption_statistic_id,
            self.cost_statistic_id,
            self.fixed_cost_statistic_id,
            STATISTICS_BACKFILL_HOURS,
            f"{fixed_unit_price} cents/kWh" if fixed_unit_price else "None",
        )

    async def import_recent_statistics(self) -> None:
        """Import recent hourly statistics with gap detection and filling."""
        _LOGGER.debug("Starting statistics import for %s", self.entity_id)

        try:
            # Fetch 15-minute interval data and aggregate to hourly
            series = await self._fetch_interval_data()

            if not series:
                _LOGGER.warning("No interval data received from API")
                return

            # Find the time range of API data (use earliest API timestamp as start)
            now_utc = datetime.now(ZoneInfo("UTC"))
            api_timestamps = [
                datetime.fromisoformat(entry.start).astimezone(ZoneInfo("UTC"))
                for entry in series
            ]
            earliest_api_timestamp = min(api_timestamps)

            # Get existing statistics covering the full API data range
            existing_consumption = await self._get_existing_statistics_in_window(
                self.consumption_statistic_id, earliest_api_timestamp, now_utc
            )
            existing_cost = await self._get_existing_statistics_in_window(
                self.cost_statistic_id, earliest_api_timestamp, now_utc
            )

            _LOGGER.debug(
                "Found %d existing consumption records, %d existing cost records in backfill window",
                len(existing_consumption),
                len(existing_cost),
            )

            # Detect gaps (missing timestamps)
            gap_series = self._detect_gaps(series, existing_consumption)

            if not gap_series:
                _LOGGER.debug("No gaps detected, all data already imported")
                return

            _LOGGER.info(
                "Detected %d missing hourly intervals, filling gaps",
                len(gap_series),
            )

            # Build statistics for gaps only
            consumption_stats, cost_stats, fixed_cost_stats = await self._build_statistics_for_gaps(
                gap_series,
                self.consumption_statistic_id,
                self.cost_statistic_id,
                self.fixed_cost_statistic_id,
            )

            # Import gap-filling statistics
            if consumption_stats:
                await self._import_consumption_statistics(consumption_stats)
                await self._import_cost_statistics(cost_stats)

                # Import fixed cost statistics if we have fixed price data
                if fixed_cost_stats:
                    await self._import_fixed_cost_statistics(fixed_cost_stats)

                _LOGGER.info(
                    "Successfully filled %d gaps in statistics for %s (fixed_cost=%s)",
                    len(consumption_stats),
                    self.entity_id,
                    "yes" if fixed_cost_stats else "no",
                )
            else:
                _LOGGER.debug("No valid gap data to import (missing electricity or prices)")

        except Exception as err:
            _LOGGER.error(
                "Error importing statistics for %s: %s",
                self.entity_id,
                err,
                exc_info=True,
            )
            raise

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

            # Get existing statistics in this date range
            now_utc = datetime.now(ZoneInfo("UTC"))
            earliest_api_timestamp = min(
                datetime.fromisoformat(entry.start).astimezone(ZoneInfo("UTC"))
                for entry in response.series
            )

            existing_consumption = await self._get_existing_statistics_in_window(
                self.consumption_statistic_id, earliest_api_timestamp, now_utc
            )

            _LOGGER.debug(
                "Found %d existing consumption records in backfill window",
                len(existing_consumption),
            )

            # Detect gaps (only import missing hours)
            gap_series = self._detect_gaps(response.series, existing_consumption)

            if not gap_series:
                _LOGGER.info("No gaps detected - all data already exists")
                return

            _LOGGER.info(
                "Detected %d missing hourly intervals to backfill",
                len(gap_series),
            )

            # Build statistics for gaps
            consumption_stats, cost_stats, fixed_cost_stats = (
                await self._build_statistics_for_gaps(
                    gap_series,
                    self.consumption_statistic_id,
                    self.cost_statistic_id,
                    self.fixed_cost_statistic_id,
                )
            )

            # Import statistics
            if consumption_stats:
                await self._import_consumption_statistics(consumption_stats)
                await self._import_cost_statistics(cost_stats)

                if fixed_cost_stats:
                    await self._import_fixed_cost_statistics(fixed_cost_stats)

                _LOGGER.info(
                    "Backfill complete: imported %d hours for %s (spot_cost=yes, fixed_cost=%s)",
                    len(consumption_stats),
                    self.entity_id,
                    "yes" if fixed_cost_stats else "no",
                )
            else:
                _LOGGER.warning("No valid data to import (missing electricity or prices)")

        except Exception as err:
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

    def _aggregate_to_hourly(
        self, quarter_series: list[MeasurementsWithSpotPriceSeries]
    ) -> list[MeasurementsWithSpotPriceSeries]:
        """Aggregate 15-minute intervals to hourly intervals.

        Args:
            quarter_series: List of 15-minute measurement intervals

        Returns:
            List of hourly measurement intervals with summed consumption and averaged prices
        """
        from collections import defaultdict

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

            # Sum electricity consumption for the hour
            hourly_electricity_sum = sum(
                q.electricity for q in quarters if q.electricity is not None
            )
            # Round to 2 decimals to match official app/API precision for hourly data
            hourly_electricity = (
                round(hourly_electricity_sum, 2) if hourly_electricity_sum else None
            )

            # Average spot prices for the hour (already includes VAT)
            valid_prices = [
                q.electricity_spot_prices_vat
                for q in quarters
                if q.electricity_spot_prices_vat is not None
            ]
            # Round to 4 decimals for price precision (used in cost calculations)
            hourly_price = (
                round(sum(valid_prices) / len(valid_prices), 4)
                if valid_prices
                else None
            )

            # Create hourly entry
            # Note: We're reusing the MeasurementsWithSpotPriceSeries structure
            # but with aggregated hourly values
            hourly_entry = MeasurementsWithSpotPriceSeries(
                start=hour_key,
                stop=quarters[-1].stop,  # End of the last quarter
                electricity=hourly_electricity if hourly_electricity else None,
                electricity_spot_prices_vat=hourly_price,
            )

            hourly_series.append(hourly_entry)

        # Deduplicate hourly entries by timestamp (shouldn't happen, but defensive)
        seen_timestamps = set()
        deduplicated_series = []
        for entry in hourly_series:
            if entry.start not in seen_timestamps:
                seen_timestamps.add(entry.start)
                deduplicated_series.append(entry)
            else:
                _LOGGER.warning(
                    "Duplicate hourly entry detected for %s - this should not happen!",
                    entry.start,
                )

        if len(deduplicated_series) < len(hourly_series):
            _LOGGER.error(
                "Removed %d duplicate hourly entries during aggregation",
                len(hourly_series) - len(deduplicated_series),
            )

        return deduplicated_series

    async def _get_last_cumulative_total(
        self, statistic_id: str
    ) -> tuple[float, datetime | None]:
        """Get the last cumulative total and timestamp from existing statistics.

        Args:
            statistic_id: The statistic ID to query

        Returns:
            Tuple of (last_sum, last_timestamp) - sum in appropriate units, timestamp or None
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
                stat = last_stats[statistic_id][0]
                last_sum = stat.get("sum", 0.0)
                last_timestamp_raw = stat.get("start")

                # Convert Unix timestamp to datetime if present
                last_timestamp = None
                if last_timestamp_raw is not None:
                    # Handle both Unix timestamp (float) and datetime objects
                    if isinstance(last_timestamp_raw, datetime):
                        last_timestamp = last_timestamp_raw
                    else:
                        # Convert Unix timestamp to datetime
                        last_timestamp = datetime.fromtimestamp(
                            last_timestamp_raw, tz=ZoneInfo("UTC")
                        )

                return safe_round(last_sum), last_timestamp

            _LOGGER.debug(
                "No existing statistics found for %s, starting from 0.0", statistic_id
            )
            return 0.0, None

        except Exception as err:
            _LOGGER.warning(
                "Error querying last statistics for %s, starting from 0.0: %s",
                statistic_id,
                err,
            )
            return 0.0, None

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

    def _build_statistics_from_intervals(
        self,
        series: list[MeasurementsWithSpotPriceSeries],
        last_consumption_cumulative: float,
        last_cost_cumulative: float,
        last_timestamp: datetime | None = None,
    ) -> tuple[list[StatisticData], list[StatisticData]]:
        """Build statistics from hourly interval data.

        Only imports intervals AFTER last_timestamp to prevent duplicate imports
        that would cause cumulative values to grow incorrectly.

        Args:
            series: List of hourly measurement intervals from API
            last_consumption_cumulative: Last known cumulative consumption (kWh)
            last_cost_cumulative: Last known cumulative cost (EUR)
            last_timestamp: Last statistics timestamp (UTC), skip data at or before this

        Returns:
            Tuple of (consumption_statistics, cost_statistics)
        """
        consumption_statistics = []
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

            # Normalize timestamp: strip microseconds and ensure it's at top of hour
            # This is CRITICAL for HA statistics deduplication
            utc_time = utc_time.replace(minute=0, second=0, microsecond=0)

            # Verify timestamp is at top of hour (should already be from aggregation)
            if utc_time.minute != 0 or utc_time.second != 0:
                _LOGGER.warning("Received non-hourly timestamp from API: %s", utc_time)
                continue

            # Filter out future data (API can return predictions)
            if utc_time > now_utc:
                future_count += 1
                continue

            # Skip data at or before last known timestamp to prevent duplicates
            if last_timestamp is not None and utc_time <= last_timestamp:
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
                    "Missing spot price for timestamp %s, skipping cost statistics",
                    utc_time,
                )
                missing_count += 1
                continue

            # Calculate hourly cost (kWh * EUR/kWh = EUR)
            hourly_cost = electricity * spot_price

            # Add to cumulative totals
            cumulative_consumption += electricity
            cumulative_cost += hourly_cost

            # Create consumption statistics data point (cumulative)
            stat_data = StatisticData(
                start=utc_time,
                state=safe_round(cumulative_consumption),
                sum=safe_round(cumulative_consumption),
            )
            consumption_statistics.append(stat_data)

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
            "Built %d NEW hourly statistics entries (skipped data at or before %s). "
            "Final cumulative: consumption=%.2f kWh, cost=%.2f EUR",
            len(consumption_statistics),
            last_timestamp.isoformat() if last_timestamp else "none",
            cumulative_consumption,
            cumulative_cost,
        )

        return consumption_statistics, cost_statistics

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

    async def _import_consumption_statistics(
        self, statistics: list[StatisticData]
    ) -> None:
        """Import consumption statistics into Home Assistant database.

        Args:
            statistics: List of StatisticData to import
        """
        metadata_kwargs = {
            "has_sum": True,
            "name": "Helen Energy Hourly Consumption Statistics",
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

    async def _import_cost_statistics(self, statistics: list[StatisticData]) -> None:
        """Import cost statistics into Home Assistant database.

        Args:
            statistics: List of StatisticData to import
        """
        metadata_kwargs = {
            "has_sum": True,
            "name": "Helen Energy Hourly Spot Prices",
            "source": DOMAIN,
            "statistic_id": self.cost_statistic_id,
            "unit_of_measurement": "EUR",
            "unit_class": None,  # Might break in 2026.11
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

    async def _import_fixed_cost_statistics(
        self, statistics: list[StatisticData]
    ) -> None:
        """Import fixed cost statistics into Home Assistant database.

        Fixed cost uses a constant unit price instead of spot prices.

        Args:
            statistics: List of StatisticData to import
        """
        metadata_kwargs = {
            "has_sum": True,
            "name": "Helen Energy Hourly Fixed Prices",
            "source": DOMAIN,
            "statistic_id": self.fixed_cost_statistic_id,
            "unit_of_measurement": "EUR",
            "unit_class": None,
        }

        if HAS_MEAN_TYPE:
            metadata_kwargs["mean_type"] = StatisticMeanType.NONE
        else:
            metadata_kwargs["has_mean"] = False

        metadata = StatisticMetaData(**metadata_kwargs)
        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug(
            "Fixed cost statistics imported successfully for %s",
            self.fixed_cost_statistic_id,
        )
