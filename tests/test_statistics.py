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
        )

        assert manager.hass == hass
        assert manager.api_client == mock_api_client
        assert manager.entity_id == "sensor.helen_monthly_consumption"
        assert (
            manager.consumption_statistic_id == "helen_energy:hourly_energy_consumption"
        )
        assert manager.cost_statistic_id == "helen_energy:hourly_cost"

    def test_convert_to_utc(self, hass: HomeAssistant, mock_api_client):
        """Test timezone conversion from Helsinki to UTC."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

        # Test with electricity value present
        entry = Mock(electricity=5.5)
        assert manager._extract_electricity_value(entry) == 5.5

        # Test with None (missing data)
        entry = Mock(electricity=None)
        assert manager._extract_electricity_value(entry) is None

    def test_extract_spot_price_value(self, hass: HomeAssistant, mock_api_client):
        """Test extracting spot price value from measurement entry."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test")

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
            hass, mock_api_client, "sensor.helen_monthly_consumption"
        )

        # Create async mock - return stats with the new statistic_id format
        test_timestamp = datetime(2024, 5, 15, 10, 0, 0, tzinfo=ZoneInfo("UTC"))

        async def async_get_stats(*args, **kwargs):
            return {
                "helen_energy:hourly_energy_consumption": [
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
                assert metadata["name"] == "Helen Energy Hourly Consumption Statistics"
                assert metadata["source"] == "helen_energy"
                assert metadata["statistic_id"] == "helen_energy:hourly_energy_consumption"
                assert metadata["unit_of_measurement"] == UnitOfEnergy.KILO_WATT_HOUR
                assert metadata["unit_class"] == "energy"

                # Version-aware mean type checking
                if has_mean_type:
                    assert metadata.get("mean_type") == StatisticMeanType.NONE
                else:
                    assert metadata["has_mean"] is False
            else:
                assert metadata.has_sum is True
                assert metadata.name == "Helen Energy Hourly Consumption Statistics"
                assert metadata.source == "helen_energy"
                assert metadata.statistic_id == "helen_energy:hourly_energy_consumption"
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
                assert metadata["name"] == "Helen Energy Hourly Total Cost Statistics"
                assert metadata["statistic_id"] == "helen_energy:hourly_cost"
                assert metadata["unit_of_measurement"] == "EUR"
                assert metadata["unit_class"] is None
            else:
                assert metadata.name == "Helen Energy Hourly Total Cost Statistics"
                assert metadata.statistic_id == "helen_energy:hourly_cost"
                assert metadata.unit_of_measurement == "EUR"
                assert metadata.unit_class is None

            # Verify statistics data
            statistics_arg = call_args[0][2]  # Third argument is statistics list
            assert statistics_arg == test_statistics
