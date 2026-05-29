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

    async def test_fetch_interval_data(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test fetching hourly interval data from API."""
        manager = HelenStatisticsManager(hass, mock_api_client, "sensor.test", "test_entry_12345678", "Helen Energy (test)")

        # Create mock hourly response (not quarters)
        mock_hourly_response = Mock()
        mock_hourly_response.series = [
            Mock(
                start="2024-05-15T10:00:00+03:00",
                stop="2024-05-15T11:00:00+03:00",
                electricity=2.0,
                electricity_spot_prices_vat=500.0,
            ),
            Mock(
                start="2024-05-15T11:00:00+03:00",
                stop="2024-05-15T12:00:00+03:00",
                electricity=1.5,
                electricity_spot_prices_vat=450.0,
            ),
            Mock(
                start="2024-05-15T12:00:00+03:00",
                stop="2024-05-15T13:00:00+03:00",
                electricity=1.8,
                electricity_spot_prices_vat=475.0,
            ),
        ]
        mock_hourly_response.resolution = "hour"
        mock_hourly_response.missing_series = []

        # Create async mock for executor job
        async def async_return(func, *args, **kwargs):
            return mock_hourly_response

        # Mock the API call
        with patch.object(hass, "async_add_executor_job", side_effect=async_return):
            series = await manager._fetch_interval_data()

        # Verify we got hourly data directly (no aggregation)
        assert len(series) == 3
        assert series[0].electricity == 2.0
        assert series[1].electricity == 1.5
        assert series[2].electricity == 1.8

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
        # Both gaps query the DB first, but the second gap then uses the first gap's
        # cumulative from memory since it's more recent than the DB result
        call_counts = {"consumption": 0, "cost": 0}

        async def mock_get_cumulative(statistic_id, timestamp):
            # Returns (cumulative, timestamp_of_last_record)
            # For a fresh system with no existing data, return None timestamp
            if "consumption" in statistic_id:
                call_counts["consumption"] += 1
                # No existing data in DB
                return 100.0, None
            else:  # cost
                call_counts["cost"] += 1
                # No existing data in DB
                return 50.0, None

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

            # Both gaps query DB first (2 gaps * 2 statistic types = 4 calls)
            # Second gap then uses first gap's cumulative from memory
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

        # Second gap (non-consecutive): uses cumulative from first gap
        # 101.5 + 2.0 = 103.5 kWh
        assert consumption_stats[1]["sum"] == 103.5
        # Cost: 57.5 + (2.0 * 6.00) = 69.5 EUR
        assert cost_stats[1]["sum"] == 69.5

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

    async def test_import_statistics_metadata(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Test that _import_statistics builds correct metadata."""
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

        with patch(
            "custom_components.helen_energy.statistics.async_add_external_statistics"
        ) as mock_import:
            await manager._import_statistics(
                manager.consumption_statistic_id,
                "Helen Energy (test) - Consumption",
                UnitOfEnergy.KILO_WATT_HOUR,
                "energy",
                test_statistics,
            )

        assert mock_import.called
        metadata = mock_import.call_args[0][1]

        try:
            from homeassistant.components.recorder.models import StatisticMeanType

            has_mean_type = True
        except ImportError:
            has_mean_type = False

        def field(meta, key):
            return meta[key] if isinstance(meta, dict) else getattr(meta, key)

        assert field(metadata, "has_sum") is True
        assert field(metadata, "name") == "Helen Energy (test) - Consumption"
        assert field(metadata, "source") == "helen_energy"
        assert field(metadata, "statistic_id") == manager.consumption_statistic_id
        assert field(metadata, "unit_of_measurement") == UnitOfEnergy.KILO_WATT_HOUR
        assert field(metadata, "unit_class") == "energy"
        if has_mean_type:
            assert field(metadata, "mean_type") == StatisticMeanType.NONE
        else:
            assert field(metadata, "has_mean") is False

        assert mock_import.call_args[0][2] == test_statistics

    async def test_fill_gaps_imports_all_three_streams(
        self, hass: HomeAssistant, mock_api_client
    ):
        """End-to-end: _fill_gaps detects gaps and imports all three streams."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
            fixed_unit_price=10.0,  # 10 cents/kWh -> 0.10 EUR/kWh
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        base_time = datetime(2024, 5, 15, 10, 0, 0, tzinfo=helsinki_tz)
        series = [
            Mock(
                start=(base_time + timedelta(hours=i)).isoformat(),
                stop=(base_time + timedelta(hours=i + 1)).isoformat(),
                electricity=2.0,
                electricity_spot_prices_vat=500.0,  # 5.00 EUR/kWh
            )
            for i in range(2)
        ]

        async def no_existing(statistic_id, start, end):
            return {}

        async def zero_cumulative(statistic_id, timestamp):
            return 0.0, None

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=no_existing
            ),
            patch.object(
                manager,
                "_get_cumulative_at_or_before_timestamp",
                side_effect=zero_cumulative,
            ),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._fill_gaps(series)

        # Three streams imported: consumption, spot cost, fixed cost
        assert mock_import.call_count == 3

        def stream(statistic_id):
            for call in mock_import.call_args_list:
                meta = call[0][1]
                sid = meta["statistic_id"] if isinstance(meta, dict) else meta.statistic_id
                if sid == statistic_id:
                    return meta, call[0][2]
            raise AssertionError(f"No import for {statistic_id}")

        def field(meta, key):
            return meta[key] if isinstance(meta, dict) else getattr(meta, key)

        cons_meta, cons_stats = stream(manager.consumption_statistic_id)
        assert field(cons_meta, "unit_of_measurement") == UnitOfEnergy.KILO_WATT_HOUR
        assert field(cons_meta, "unit_class") == "energy"
        assert [s["sum"] for s in cons_stats] == [2.0, 4.0]

        spot_meta, spot_stats = stream(manager.cost_statistic_id)
        assert field(spot_meta, "unit_of_measurement") == "EUR"
        assert field(spot_meta, "unit_class") is None
        assert [s["sum"] for s in spot_stats] == [10.0, 20.0]

        fixed_meta, fixed_stats = stream(manager.fixed_cost_statistic_id)
        assert field(fixed_meta, "unit_of_measurement") == "EUR"
        assert [s["sum"] for s in fixed_stats] == [0.2, 0.4]
