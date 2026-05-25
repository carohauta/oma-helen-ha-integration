"""Tests for Helen Energy statistics manager."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from custom_components.helen_energy.statistics import HelenStatisticsManager


@pytest.fixture
def mock_api_client():
    """Mock Helen API client for testing."""
    mock_client = Mock()
    mock_client.close = Mock()
    return mock_client


@pytest.fixture
def mock_measurement_series():
    """Create mock 15-minute measurement series spanning 3 hours (12 quarters)."""
    helsinki_tz = ZoneInfo("Europe/Helsinki")
    base_time = datetime(2024, 5, 15, 10, 0, 0, tzinfo=helsinki_tz)

    series = []
    # Create 12 x 15-minute entries (3 hours worth)
    # Each quarter has 0.5 kWh, so 4 quarters = 2.0 kWh per hour
    for i in range(12):
        start_time = base_time + timedelta(minutes=15 * i)
        series.append(
            Mock(
                start=start_time.isoformat(),
                stop=(start_time + timedelta(minutes=15)).isoformat(),
                electricity=0.5,  # 0.5 kWh per 15 minutes (2.0 kWh per hour)
                electricity_spot_prices_vat=500.0,  # 5.00 EUR/kWh (cents, constant for test)
            )
        )

    return series


@pytest.fixture
def mock_hourly_series():
    """Create mock hourly measurement series spanning 3 hours.

    This is used for tests that call _build_statistics_from_intervals directly,
    since aggregation from 15-min to hourly happens in _fetch_interval_data.
    """
    helsinki_tz = ZoneInfo("Europe/Helsinki")
    base_time = datetime(2024, 5, 15, 10, 0, 0, tzinfo=helsinki_tz)

    series = []
    # Create 3 hourly entries
    for i in range(3):
        start_time = base_time + timedelta(hours=i)
        series.append(
            Mock(
                start=start_time.isoformat(),
                stop=(start_time + timedelta(hours=1)).isoformat(),
                electricity=2.0,  # 2.0 kWh per hour
                electricity_spot_prices_vat=500.0,  # 5.00 EUR/kWh (cents)
            )
        )

    return series


@pytest.fixture
def mock_measurement_response(mock_measurement_series):
    """Mock API response for 15-minute measurements."""
    mock_response = Mock()
    mock_response.series = mock_measurement_series
    mock_response.resolution = "quarter"
    mock_response.missing_series = []
    return mock_response


class TestHelenStatisticsManager:
    """Test the HelenStatisticsManager class."""

    def test_initialization(self, hass: HomeAssistant, mock_api_client):
        """Test statistics manager initialization."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        assert manager.hass == hass
        assert manager.api_client == mock_api_client
        assert manager.entity_id == "sensor.helen_monthly_consumption"
        assert manager.config_entry_title == "Helen Energy (test)"
        assert (
            manager.consumption_statistic_id == "helen_energy:hourly_energy_consumption_test_ent"
        )
        assert manager.cost_statistic_id == "helen_energy:hourly_cost_spot_test_ent"

    def test_convert_to_utc(self, hass: HomeAssistant, mock_api_client):
        """Test timezone conversion from Helsinki to UTC."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Test winter time (UTC+2)
        helsinki_winter = "2024-01-15T12:00:00+02:00"
        utc_result = manager._convert_to_utc(helsinki_winter)
        assert utc_result.tzinfo == ZoneInfo("UTC")
        assert utc_result.hour == 10  # 12:00 + 2 = 10:00 UTC

        # Test summer time (UTC+3)
        helsinki_summer = "2024-06-15T12:00:00+03:00"
        utc_result = manager._convert_to_utc(helsinki_summer)
        assert utc_result.tzinfo == ZoneInfo("UTC")
        assert utc_result.hour == 9  # 12:00 +3 = 09:00 UTC

    def test_extract_electricity_value(self, hass: HomeAssistant, mock_api_client):
        """Test extracting electricity value from measurement entry."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Test with electricity value present
        entry = Mock(electricity=5.5)
        assert manager._extract_electricity_value(entry) == 5.5

        # Test with None (missing data)
        entry = Mock(electricity=None)
        assert manager._extract_electricity_value(entry) is None

    def test_extract_spot_price_value(self, hass: HomeAssistant, mock_api_client):
        """Test extracting spot price value from measurement entry."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Test with spot price value present (in cents)
        entry = Mock(electricity_spot_prices_vat=500.0)  # 500 cents = 5.00 EUR
        assert manager._extract_spot_price_value(entry) == 5.0

        # Test with None (missing data)
        entry = Mock(electricity_spot_prices_vat=None)
        assert manager._extract_spot_price_value(entry) is None

    def test_build_statistics_cumulative_calculation(
        self, hass: HomeAssistant, mock_api_client, mock_hourly_series
    ):
        """Test cumulative total calculation from hourly interval data."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Start with cumulative total of 100 kWh and 0 EUR
        last_consumption = 100.0
        last_cost = 0.0

        consumption_stats, cost_stats = (
            manager._build_statistics_from_intervals(
                mock_hourly_series, last_consumption, last_cost
            )
        )

        # Should have 3 hourly entries
        assert len(consumption_stats) == 3

        # Each hour adds 2.0 kWh, so final cumulative should be 100 + 6.0 = 106.0
        assert consumption_stats[-1]["sum"] == 106.0
        assert consumption_stats[-1]["state"] == 106.0

        # First hourly entry should be 102.0
        assert consumption_stats[0]["sum"] == 102.0

        # Each statistic should have timestamps at top of hour
        for stat in consumption_stats:
            assert stat["start"].minute == 0
            assert stat["start"].second == 0

        # Verify all are StatisticData dicts with proper structure
        for stat in consumption_stats:
            assert isinstance(stat, dict)
            assert "start" in stat
            assert "sum" in stat
            assert "state" in stat
            assert (
                stat["sum"] == stat["state"]
            )  # For TOTAL sensors, sum and state are the same

    def test_build_statistics_with_prices_and_costs(
        self, hass: HomeAssistant, mock_api_client, mock_hourly_series
    ):
        """Test building consumption and cost statistics."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Start with 100 kWh and 50 EUR
        last_consumption = 100.0
        last_cost = 50.0

        consumption_stats, cost_stats = (
            manager._build_statistics_from_intervals(
                mock_hourly_series, last_consumption, last_cost
            )
        )

        # Should have 3 hourly entries for both
        assert len(consumption_stats) == 3
        assert len(cost_stats) == 3

        # Consumption: 3 hours × 2.0 kWh = 6.0 kWh total
        assert consumption_stats[-1]["sum"] == 106.0

        # Cost: 3 hours × (2.0 kWh × 5.00 EUR/kWh) = 30.0 EUR total
        assert cost_stats[-1]["sum"] == 80.0

        # Verify timestamps match across both
        for i in range(3):
            assert consumption_stats[i]["start"] == cost_stats[i]["start"]

    def test_build_statistics_handles_missing_data(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test handling of missing data (None values)."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        base_time = datetime(2024, 5, 15, 0, 0, 0, tzinfo=helsinki_tz)

        series = [
            Mock(
                start=base_time.isoformat(),
                stop=(base_time + timedelta(hours=1)).isoformat(),
                electricity=0.5,
                electricity_spot_prices_vat=500.0,
            ),
            Mock(
                start=(base_time + timedelta(hours=1)).isoformat(),
                stop=(base_time + timedelta(hours=2)).isoformat(),
                electricity=None,  # Missing data
                electricity_spot_prices_vat=500.0,
            ),
            Mock(
                start=(base_time + timedelta(hours=2)).isoformat(),
                stop=(base_time + timedelta(hours=3)).isoformat(),
                electricity=0.5,
                electricity_spot_prices_vat=500.0,
            ),
        ]

        consumption_stats, cost_stats = (
            manager._build_statistics_from_intervals(series, 0.0, 0.0)
        )

        # Should have 2 hourly statistics entries (skipped the None entry)
        assert len(consumption_stats) == 2

        # Cumulative should skip None and sum valid entries: 0.5 + 0.5 = 1.0
        assert consumption_stats[-1]["sum"] == 1.0

    def test_build_statistics_filters_future_data(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test filtering of future data (critical - API returns predictions)."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        helsinki_tz = ZoneInfo("Europe/Helsinki")

        # Create series with past and future data (rounded to hourly)
        now = datetime.now(helsinki_tz)
        past_time = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        future_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(
            hours=1
        )

        series = [
            Mock(
                start=past_time.isoformat(),
                stop=(past_time + timedelta(hours=1)).isoformat(),
                electricity=0.5,
                electricity_spot_prices_vat=500.0,
            ),
            Mock(
                start=future_time.isoformat(),  # Future data - should be filtered
                stop=(future_time + timedelta(hours=1)).isoformat(),
                electricity=0.5,
                electricity_spot_prices_vat=500.0,
            ),
            Mock(
                start=(future_time + timedelta(hours=1)).isoformat(),  # Also future
                stop=(future_time + timedelta(hours=2)).isoformat(),
                electricity=0.5,
                electricity_spot_prices_vat=500.0,
            ),
        ]

        consumption_stats, cost_stats = (
            manager._build_statistics_from_intervals(series, 0.0, 0.0)
        )

        # Should have only 1 statistics entry (filtered out 2 future entries)
        assert len(consumption_stats) == 1

        # Cumulative should only include past data
        assert consumption_stats[-1]["sum"] == 0.5

    def test_build_statistics_timezone_handling(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test proper timezone conversion in statistics."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        helsinki_time = datetime(2024, 1, 15, 12, 0, 0, tzinfo=helsinki_tz)

        series = [
            Mock(
                start=helsinki_time.isoformat(),
                stop=(helsinki_time + timedelta(hours=1)).isoformat(),
                electricity=0.5,
                electricity_spot_prices_vat=500.0,
            )
        ]

        consumption_stats, cost_stats = (
            manager._build_statistics_from_intervals(series, 0.0, 0.0)
        )

        # Verify the timestamp is in UTC
        assert consumption_stats[0]["start"].tzinfo == ZoneInfo("UTC")

        # Helsinki noon in winter (UTC+2) should be 10:00 UTC
        assert consumption_stats[0]["start"].hour == 10

    def test_build_statistics_skips_data_before_last_timestamp(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test that statistics import skips data at or before last known timestamp."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        base_time = datetime(2024, 5, 15, 0, 0, 0, tzinfo=helsinki_tz)

        # Create 3 hourly entries
        series = [
            Mock(
                start=base_time.isoformat(),
                stop=(base_time + timedelta(hours=1)).isoformat(),
                electricity=1.0,
                electricity_spot_prices_vat=500.0,
            ),
            Mock(
                start=(base_time + timedelta(hours=1)).isoformat(),
                stop=(base_time + timedelta(hours=2)).isoformat(),
                electricity=2.0,
                electricity_spot_prices_vat=500.0,
            ),
            Mock(
                start=(base_time + timedelta(hours=2)).isoformat(),
                stop=(base_time + timedelta(hours=3)).isoformat(),
                electricity=3.0,
                electricity_spot_prices_vat=500.0,
            ),
        ]

        # Set last_timestamp to first hour (in UTC)
        last_timestamp = base_time.astimezone(ZoneInfo("UTC")).replace(
            minute=0, second=0, microsecond=0
        )

        consumption_stats, cost_stats = (
            manager._build_statistics_from_intervals(
                series, 100.0, 50.0, last_timestamp
            )
        )

        # Should only import hours 2 and 3 (skipped hour 1 at last_timestamp)
        assert len(consumption_stats) == 2

        # First imported stat should be for hour 2 (electricity=2.0)
        # Starting from cumulative of 100.0
        assert consumption_stats[0]["sum"] == 102.0

        # Second imported stat should be for hour 3 (electricity=3.0)
        assert consumption_stats[1]["sum"] == 105.0

    async def test_fetch_interval_data(
        self, hass: HomeAssistant, mock_api_client, mock_measurement_response
    ):
        """Test fetching hourly interval data from API."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Create async mock for executor job
        async def async_return(func, *args, **kwargs):
            return mock_measurement_response

        # Mock the API call
        with patch.object(hass, "async_add_executor_job", side_effect=async_return):
            series = await manager._fetch_interval_data()

        # Verify we got the series data (3 hourly entries)
        assert len(series) == 3
        assert series[0].electricity == 2.0

    async def test_get_last_cumulative_total_no_existing_stats(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test getting last cumulative total when no statistics exist."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Mock get_instance and get_last_statistics to return empty result
        mock_instance = Mock()
        mock_instance.async_add_executor_job.return_value = {}

        with patch(
            "custom_components.helen_energy.statistics.get_instance",
            return_value=mock_instance,
        ):
            last_cumulative, last_timestamp = await manager._get_last_cumulative_total(
                manager.consumption_statistic_id
            )

        # Should return 0.0 and None when no statistics exist
        assert last_cumulative == 0.0
        assert last_timestamp is None

    async def test_get_last_cumulative_total_with_existing_stats(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test getting last cumulative total from existing statistics."""
        manager = HelenStatisticsManager(
            hass, mock_api_client, "sensor.helen_monthly_consumption", "test_entry_12345678", "Helen Energy (test)"
        )

        # Create async mock - return stats with the new statistic_id format
        test_timestamp = datetime(2024, 5, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC"))

        async def async_get_stats(*args, **kwargs):
            return {
                "helen_energy:hourly_energy_consumption_test_ent": [
                    {"sum": 1234.56, "start": test_timestamp}
                ]
            }

        # Mock get_instance and get_last_statistics to return existing stat
        mock_instance = Mock()
        mock_instance.async_add_executor_job = Mock(side_effect=async_get_stats)

        with patch(
            "custom_components.helen_energy.statistics.get_instance",
            return_value=mock_instance,
        ):
            last_cumulative, last_timestamp = await manager._get_last_cumulative_total(
                manager.consumption_statistic_id
            )

        # Should return the last sum value and timestamp
        assert last_cumulative == 1234.56
        assert last_timestamp == test_timestamp

    async def test_import_consumption_statistics_metadata(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test that consumption statistics import uses correct metadata."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        test_statistics = [
            {
                "start": datetime(2024, 5, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
                "state": 100.0,
                "sum": 100.0,
            }
        ]

        # Mock async_add_external_statistics
        with patch(
            "custom_components.helen_energy.statistics.async_add_external_statistics"
        ) as mock_import:
            await manager._import_consumption_statistics(test_statistics)

            # Verify async_add_external_statistics was called
            assert mock_import.called
            call_args = mock_import.call_args

            # Verify metadata (dict or object depending on HA version)
            metadata = call_args[0][1]  # Second argument is metadata

            # Check if StatisticMeanType is available
            try:
                from homeassistant.components.recorder.models import StatisticMeanType

                has_mean_type = True
            except ImportError:
                has_mean_type = False

            if isinstance(metadata, dict):
                assert metadata["has_sum"] is True
                assert metadata["name"] == "Helen Energy (test) - Consumption"
                assert metadata["source"] == "helen_energy"
                assert metadata["statistic_id"] == "helen_energy:hourly_energy_consumption_test_ent"
                assert metadata["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR
                assert metadata["unit_class"] == "energy"

                # Version-aware mean type checking
                if has_mean_type:
                    assert metadata.get("mean_type") == StatisticMeanType.NONE
                else:
                    assert metadata["has_mean"] is False
            else:
                assert metadata.has_sum is True
                assert metadata.name == "Helen Energy (test) - Consumption"
                assert metadata.source == "helen_energy"
                assert metadata.statistic_id == "helen_energy:hourly_energy_consumption_test_ent"
                assert metadata.unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
                assert metadata.unit_class == "energy"

                # Version-aware mean type checking
                if has_mean_type:
                    assert metadata.mean_type == StatisticMeanType.NONE
                else:
                    assert metadata.has_mean is False

            # Verify statistics data
            statistics_arg = call_args[0][2]  # Third argument is statistics list
            assert statistics_arg == test_statistics

    async def test_import_cost_statistics_metadata(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test that cost statistics import uses correct metadata."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        test_statistics = [
            {
                "start": datetime(2024, 5, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
                "state": 50.0,
                "sum": 50.0,
            }
        ]

        # Mock async_add_external_statistics
        with patch(
            "custom_components.helen_energy.statistics.async_add_external_statistics"
        ) as mock_import:
            await manager._import_cost_statistics(test_statistics)

            # Verify async_add_external_statistics was called
            assert mock_import.called
            call_args = mock_import.call_args

            # Verify metadata (dict or object depending on HA version)
            metadata = call_args[0][1]  # Second argument is metadata

            if isinstance(metadata, dict):
                assert metadata["name"] == "Helen Energy (test) - Spot Prices"
                assert metadata["statistic_id"] == "helen_energy:hourly_cost_spot_test_ent"
                assert metadata["unit_of_measurement"] == "EUR"
                assert metadata["unit_class"] is None
            else:
                assert metadata.name == "Helen Energy (test) - Spot Prices"
                assert metadata.statistic_id == "helen_energy:hourly_cost_spot_test_ent"
                assert metadata.unit_of_measurement == "EUR"
                assert metadata.unit_class is None

            # Verify statistics data
            statistics_arg = call_args[0][2]  # Third argument is statistics list
            assert statistics_arg == test_statistics

    async def test_get_existing_statistics_in_window(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test getting existing statistics in a time window."""
        manager = HelenStatisticsManager(
            hass, mock_api_client, "sensor.helen_monthly_consumption", "test_entry_12345678", "Helen Energy (test)"
        )

        start_time = datetime(2024, 5, 15, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
        end_time = datetime(2024, 5, 15, 3, 0, 0, tzinfo=ZoneInfo("UTC"))

        # Mock statistics_during_period to return some existing data
        async def async_stats_query(*args, **kwargs):
            return {
                "helen_energy:hourly_energy_consumption_test_ent": [
                    {"start": start_time.timestamp(), "sum": 10.0},
                    {
                        "start": (start_time + timedelta(hours=1)).timestamp(),
                        "sum": 12.0,
                    },
                    # Skip hour 2 (gap)
                ]
            }

        mock_instance = Mock()
        mock_instance.async_add_executor_job = Mock(side_effect=async_stats_query)

        with patch(
            "custom_components.helen_energy.statistics.get_instance",
            return_value=mock_instance,
        ):
            existing = await manager._get_existing_statistics_in_window(
                manager.consumption_statistic_id, start_time, end_time
            )

        # Should have 2 timestamps, missing hour 2
        assert len(existing) == 2
        assert start_time in existing
        assert start_time + timedelta(hours=1) in existing
        assert start_time + timedelta(hours=2) not in existing  # Gap
        assert existing[start_time] == 10.0
        assert existing[start_time + timedelta(hours=1)] == 12.0

    def test_detect_gaps(self, hass: HomeAssistant, mock_api_client):
        """Test gap detection logic."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        base_time = datetime(2024, 5, 15, 0, 0, 0, tzinfo=helsinki_tz)

        # Create API series with 4 hours
        api_series = []
        for i in range(4):
            start_time = base_time + timedelta(hours=i)
            api_series.append(
                Mock(
                    start=start_time.isoformat(),
                    stop=(start_time + timedelta(hours=1)).isoformat(),
                    electricity=1.0,
                    electricity_spot_prices_vat=500.0,
                )
            )

        # Existing statistics have hours 0 and 2 (missing 1 and 3)
        base_time_utc = base_time.astimezone(ZoneInfo("UTC")).replace(
            minute=0, second=0, microsecond=0
        )
        existing_timestamps = {
            base_time_utc: 10.0,
            base_time_utc + timedelta(hours=2): 12.0,
        }

        # Detect gaps
        gaps = manager._detect_gaps(api_series, existing_timestamps)

        # Should detect 2 gaps (hours 1 and 3)
        assert len(gaps) == 2
        gap_times = [
            datetime.fromisoformat(g.start)
            .astimezone(ZoneInfo("UTC"))
            .replace(minute=0, second=0, microsecond=0)
            for g in gaps
        ]
        assert base_time_utc + timedelta(hours=1) in gap_times
        assert base_time_utc + timedelta(hours=3) in gap_times

    async def test_get_cumulative_at_or_before_timestamp(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test getting cumulative value before a timestamp."""
        manager = HelenStatisticsManager(
            hass, mock_api_client, "sensor.helen_monthly_consumption", "test_entry_12345678", "Helen Energy (test)"
        )

        query_time = datetime(2024, 5, 15, 5, 0, 0, tzinfo=ZoneInfo("UTC"))

        # Mock statistics_during_period
        async def async_stats_query(*args, **kwargs):
            # Return records up to query_time
            return {
                "helen_energy:hourly_energy_consumption_test_ent": [
                    {
                        "start": datetime(
                            2024, 5, 15, 0, 0, 0, tzinfo=ZoneInfo("UTC")
                        ).timestamp(),
                        "sum": 10.0,
                    },
                    {
                        "start": datetime(
                            2024, 5, 15, 3, 0, 0, tzinfo=ZoneInfo("UTC")
                        ).timestamp(),
                        "sum": 15.0,
                    },
                ]
            }

        mock_instance = Mock()
        mock_instance.async_add_executor_job = Mock(side_effect=async_stats_query)

        with patch(
            "custom_components.helen_energy.statistics.get_instance",
            return_value=mock_instance,
        ):
            cumulative, timestamp = await manager._get_cumulative_at_or_before_timestamp(
                manager.consumption_statistic_id, query_time
            )

        # Should get the last record before query_time (hour 3)
        assert cumulative == 15.0
        assert timestamp == datetime(2024, 5, 15, 3, 0, 0, tzinfo=ZoneInfo("UTC"))

    async def test_build_statistics_for_gaps(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test building statistics for gaps with correct cumulative values."""
        manager = HelenStatisticsManager(
            hass, mock_api_client, "sensor.helen_monthly_consumption", "test_entry_12345678", "Helen Energy (test)"
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        base_time = datetime(2024, 5, 15, 10, 0, 0, tzinfo=helsinki_tz)

        # Create gap series (2 consecutive missing hours)
        gap_series = [
            Mock(
                start=base_time.isoformat(),
                stop=(base_time + timedelta(hours=1)).isoformat(),
                electricity=1.5,
                electricity_spot_prices_vat=500.0,  # 5.00 EUR/kWh
            ),
            Mock(
                start=(base_time + timedelta(hours=1)).isoformat(),
                stop=(base_time + timedelta(hours=2)).isoformat(),
                electricity=2.0,
                electricity_spot_prices_vat=600.0,  # 6.00 EUR/kWh
            ),
        ]

        # Mock _get_cumulative_at_or_before_timestamp
        # For consecutive gaps, only the first gap queries the DB
        # Subsequent consecutive gaps use the cumulative from the previous gap
        async def mock_get_cumulative(statistic_id, timestamp):
            if "consumption" in statistic_id:
                # Only called once for the first gap
                return 100.0, timestamp
            else:  # cost
                # Only called once for the first gap
                return 50.0, timestamp

        with patch.object(
            manager,
            "_get_cumulative_at_or_before_timestamp",
            side_effect=mock_get_cumulative,
        ) as mock_cumulative:
            consumption_stats, cost_stats, fixed_cost_stats = await manager._build_statistics_for_gaps(
                gap_series,
                manager.consumption_statistic_id,
                manager.cost_statistic_id,
                manager.fixed_cost_statistic_id,
            )

            # Verify only called once per statistic type (for the first gap)
            # Second gap is consecutive, so it chains from the first
            assert mock_cumulative.call_count == 2  # consumption + cost for first gap

        # Should have 2 statistics entries
        assert len(consumption_stats) == 2
        assert len(cost_stats) == 2
        # No fixed cost stats (no fixed_unit_price configured)
        assert len(fixed_cost_stats) == 0

        # First gap: 100.0 + 1.5 = 101.5 kWh
        assert consumption_stats[0]["sum"] == 101.5
        # Cost: 50.0 + (1.5 * 5.00) = 57.5 EUR
        assert cost_stats[0]["sum"] == 57.5

        # Second gap (consecutive, chained from first): 101.5 + 2.0 = 103.5 kWh
        assert consumption_stats[1]["sum"] == 103.5
        # Cost: 57.5 + (2.0 * 6.00) = 69.5 EUR
        assert cost_stats[1]["sum"] == 69.5

    async def test_build_statistics_for_gaps_non_consecutive(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test building statistics for non-consecutive gaps."""
        manager = HelenStatisticsManager(
            hass, mock_api_client, "sensor.helen_monthly_consumption", "test_entry_12345678", "Helen Energy (test)"
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        base_time = datetime(2024, 5, 15, 10, 0, 0, tzinfo=helsinki_tz)

        # Create gap series with a 2-hour break between gaps
        gap_series = [
            Mock(
                start=base_time.isoformat(),
                stop=(base_time + timedelta(hours=1)).isoformat(),
                electricity=1.5,
                electricity_spot_prices_vat=500.0,  # 5.00 EUR/kWh
            ),
            Mock(
                start=(base_time + timedelta(hours=3)).isoformat(),  # 3 hours later
                stop=(base_time + timedelta(hours=4)).isoformat(),
                electricity=2.0,
                electricity_spot_prices_vat=600.0,  # 6.00 EUR/kWh
            ),
        ]

        # Mock _get_cumulative_at_or_before_timestamp
        # For non-consecutive gaps, each gap queries the DB
        call_counts = {"consumption": 0, "cost": 0}

        async def mock_get_cumulative(statistic_id, timestamp):
            if "consumption" in statistic_id:
                call_counts["consumption"] += 1
                if call_counts["consumption"] == 1:
                    # First gap: cumulative before = 100.0
                    return 100.0, timestamp
                else:
                    # Second gap (non-consecutive): cumulative before = 110.0
                    # (assuming 110.0 kWh accumulated in the 2 hours between gaps)
                    return 110.0, timestamp
            else:  # cost
                call_counts["cost"] += 1
                if call_counts["cost"] == 1:
                    # First gap: cumulative before = 50.0
                    return 50.0, timestamp
                else:
                    # Second gap: cumulative before = 60.0
                    return 60.0, timestamp

        with patch.object(
            manager,
            "_get_cumulative_at_or_before_timestamp",
            side_effect=mock_get_cumulative,
        ) as mock_cumulative:
            consumption_stats, cost_stats, fixed_cost_stats = await manager._build_statistics_for_gaps(
                gap_series,
                manager.consumption_statistic_id,
                manager.cost_statistic_id,
                manager.fixed_cost_statistic_id,
            )

            # Verify called twice per statistic type (once for each gap)
            assert mock_cumulative.call_count == 4  # 2 gaps * 2 statistic types

        # Should have 2 statistics entries
        assert len(consumption_stats) == 2
        assert len(cost_stats) == 2
        # No fixed cost stats (no fixed_unit_price configured)
        assert len(fixed_cost_stats) == 0

        # First gap: 100.0 + 1.5 = 101.5 kWh
        assert consumption_stats[0]["sum"] == 101.5
        # Cost: 50.0 + (1.5 * 5.00) = 57.5 EUR
        assert cost_stats[0]["sum"] == 57.5

        # Second gap (non-consecutive): 110.0 + 2.0 = 112.0 kWh
        assert consumption_stats[1]["sum"] == 112.0
        # Cost: 60.0 + (2.0 * 6.00) = 72.0 EUR
        assert cost_stats[1]["sum"] == 72.0

    async def test_build_statistics_for_gaps_with_fixed_price(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test building statistics with fixed unit price calculates fixed cost."""
        # Create manager with fixed unit price (10 cents/kWh = 0.10 EUR/kWh)
        manager = HelenStatisticsManager(
            hass, mock_api_client, "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
            fixed_unit_price=10.0,  # 10 cents/kWh
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        base_time = datetime(2024, 5, 15, 10, 0, 0, tzinfo=helsinki_tz)

        # Create gap series
        gap_series = [
            Mock(
                start=base_time.isoformat(),
                stop=(base_time + timedelta(hours=1)).isoformat(),
                electricity=1.5,  # 1.5 kWh
                electricity_spot_prices_vat=500.0,  # 5.00 EUR/kWh (spot price)
            ),
            Mock(
                start=(base_time + timedelta(hours=1)).isoformat(),
                stop=(base_time + timedelta(hours=2)).isoformat(),
                electricity=2.0,  # 2.0 kWh
                electricity_spot_prices_vat=600.0,  # 6.00 EUR/kWh (spot price)
            ),
        ]

        # Mock cumulative values
        async def mock_get_cumulative(statistic_id, timestamp):
            if "consumption" in statistic_id:
                return 100.0, timestamp
            elif "fixed" in statistic_id:
                return 10.0, timestamp  # Fixed cost cumulative
            else:  # spot cost
                return 50.0, timestamp

        with patch.object(
            manager,
            "_get_cumulative_at_or_before_timestamp",
            side_effect=mock_get_cumulative,
        ):
            consumption_stats, cost_stats, fixed_cost_stats = await manager._build_statistics_for_gaps(
                gap_series,
                manager.consumption_statistic_id,
                manager.cost_statistic_id,
                manager.fixed_cost_statistic_id,
            )

        # Should have 2 statistics entries for each type
        assert len(consumption_stats) == 2
        assert len(cost_stats) == 2
        assert len(fixed_cost_stats) == 2

        # First gap: consumption = 100.0 + 1.5 = 101.5 kWh
        assert consumption_stats[0]["sum"] == 101.5
        # Spot cost: 50.0 + (1.5 * 5.00) = 57.5 EUR
        assert cost_stats[0]["sum"] == 57.5
        # Fixed cost: 10.0 + (1.5 * 0.10) = 10.15 EUR
        assert fixed_cost_stats[0]["sum"] == 10.15

        # Second gap: consumption = 101.5 + 2.0 = 103.5 kWh
        assert consumption_stats[1]["sum"] == 103.5
        # Spot cost: 57.5 + (2.0 * 6.00) = 69.5 EUR
        assert cost_stats[1]["sum"] == 69.5
        # Fixed cost: 10.15 + (2.0 * 0.10) = 10.35 EUR
        assert fixed_cost_stats[1]["sum"] == 10.35

    async def test_import_fixed_cost_statistics_metadata(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test that fixed cost statistics import uses correct metadata."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
            fixed_unit_price=10.0,
        )

        test_statistics = [
            {
                "start": datetime(2024, 5, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
                "state": 50.0,
                "sum": 50.0,
            }
        ]

        # Mock async_add_external_statistics
        with patch(
            "custom_components.helen_energy.statistics.async_add_external_statistics"
        ) as mock_import:
            await manager._import_fixed_cost_statistics(test_statistics)

            # Verify async_add_external_statistics was called
            assert mock_import.called
            call_args = mock_import.call_args

            # Verify metadata (dict or object depending on HA version)
            metadata = call_args[0][1]  # Second argument is metadata

            if isinstance(metadata, dict):
                assert metadata["name"] == "Helen Energy (test) - Fixed Prices"
                assert metadata["statistic_id"] == "helen_energy:hourly_cost_fixed_test_ent"
                assert metadata["unit_of_measurement"] == "EUR"
                assert metadata["unit_class"] is None
            else:
                assert metadata.name == "Helen Energy (test) - Fixed Prices"
                assert metadata.statistic_id == "helen_energy:hourly_cost_fixed_test_ent"
                assert metadata.unit_of_measurement == "EUR"
                assert metadata.unit_class is None

            # Verify statistics data
            statistics_arg = call_args[0][2]  # Third argument is statistics list
            assert statistics_arg == test_statistics
