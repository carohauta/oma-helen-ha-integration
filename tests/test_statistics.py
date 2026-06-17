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

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=no_existing
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

    async def test_fill_gaps_extends_from_existing_db(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Existing DB sum is the starting point; walk continues consecutively."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        utc = ZoneInfo("UTC")
        # Recent timestamps so age stays within the wait window
        now_hour = datetime.now(utc).replace(minute=0, second=0, microsecond=0)
        last_db_hour_utc = now_hour - timedelta(hours=5)

        # API series covers 4 hours after last_db_hour
        series = []
        for i in range(1, 5):
            t_utc = last_db_hour_utc + timedelta(hours=i)
            t_hki = t_utc.astimezone(helsinki_tz)
            series.append(
                Mock(
                    start=t_hki.isoformat(),
                    stop=(t_hki + timedelta(hours=1)).isoformat(),
                    electricity=1.0,
                    electricity_spot_prices_vat=100.0,  # 1 EUR/kWh
                )
            )

        existing = {
            manager.consumption_statistic_id: {last_db_hour_utc: 100.0},
            manager.cost_statistic_id: {last_db_hour_utc: 50.0},
        }

        async def fake_existing(statistic_id, start, end):
            return existing.get(statistic_id, {})

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=fake_existing
            ),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._fill_gaps(series)

        # Find the consumption import
        cons_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.consumption_statistic_id
        )
        cons_stats = cons_call[0][2]
        # Starts at 100 + 1.0 (first new hour), then +1.0 each
        assert [s["sum"] for s in cons_stats] == [101.0, 102.0, 103.0, 104.0]

        cost_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.cost_statistic_id
        )
        cost_stats = cost_call[0][2]
        # Spot price = 1.0 EUR/kWh, electricity = 1.0 kWh -> +1.0 per hour from 50.0
        assert [s["sum"] for s in cost_stats] == [51.0, 52.0, 53.0, 54.0]

    async def test_fill_gaps_stops_at_recent_missing_hour(
        self, hass: HomeAssistant, mock_api_client
    ):
        """A missing hour within the wait window halts the walk; no later hours written."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        utc = ZoneInfo("UTC")
        now_hour = datetime.now(utc).replace(minute=0, second=0, microsecond=0)
        last_db_hour_utc = now_hour - timedelta(hours=4)

        # API has data at hour +1 and +3, missing at +2 (which is recent)
        def make(t_utc, electricity, spot):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=electricity,
                electricity_spot_prices_vat=spot,
            )

        series = [
            make(last_db_hour_utc + timedelta(hours=1), 1.0, 100.0),
            # hour +2 missing entirely
            make(last_db_hour_utc + timedelta(hours=3), 1.0, 100.0),
        ]

        existing = {manager.consumption_statistic_id: {last_db_hour_utc: 100.0}}

        async def fake_existing(statistic_id, start, end):
            return existing.get(statistic_id, {})

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=fake_existing
            ),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._fill_gaps(series)

        cons_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.consumption_statistic_id
        )
        cons_stats = cons_call[0][2]
        # Only hour +1 written; walk stops at recent missing hour +2
        assert [s["sum"] for s in cons_stats] == [101.0]

    async def test_fill_gaps_zero_fills_old_missing_hour(
        self, hass: HomeAssistant, mock_api_client
    ):
        """A missing hour older than the wait threshold is zero-filled and the walk continues."""
        from custom_components.helen_energy.const import STATISTICS_MAX_GAP_WAIT_HOURS

        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        utc = ZoneInfo("UTC")
        now_hour = datetime.now(utc).replace(minute=0, second=0, microsecond=0)
        # Anchor far enough back that the missing hour is older than the threshold
        last_db_hour_utc = now_hour - timedelta(hours=STATISTICS_MAX_GAP_WAIT_HOURS + 10)

        def make(t_utc, electricity, spot):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=electricity,
                electricity_spot_prices_vat=spot,
            )

        # Hour +1 has data, +2 missing (but very old → zero-fill), +3 has data again
        series = [
            make(last_db_hour_utc + timedelta(hours=1), 1.0, 100.0),
            make(last_db_hour_utc + timedelta(hours=3), 2.0, 100.0),
        ]

        existing = {manager.consumption_statistic_id: {last_db_hour_utc: 100.0}}

        async def fake_existing(statistic_id, start, end):
            return existing.get(statistic_id, {})

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=fake_existing
            ),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._fill_gaps(series)

        cons_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.consumption_statistic_id
        )
        cons_stats = cons_call[0][2]
        # +1: 100 + 1 = 101, +2: zero-filled (no change), +3: 101 + 2 = 103
        assert [s["sum"] for s in cons_stats] == [101.0, 101.0, 103.0]

    async def test_fill_gaps_noop_when_db_already_caught_up(
        self, hass: HomeAssistant, mock_api_client
    ):
        """If DB is already past the latest API hour, nothing is written."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        helsinki_tz = ZoneInfo("Europe/Helsinki")
        utc = ZoneInfo("UTC")
        now_hour = datetime.now(utc).replace(minute=0, second=0, microsecond=0)
        api_hour_utc = now_hour - timedelta(hours=2)
        api_hour_hki = api_hour_utc.astimezone(helsinki_tz)
        series = [
            Mock(
                start=api_hour_hki.isoformat(),
                stop=(api_hour_hki + timedelta(hours=1)).isoformat(),
                electricity=1.0,
                electricity_spot_prices_vat=100.0,
            )
        ]

        # DB already has the latest API hour
        existing = {manager.consumption_statistic_id: {api_hour_utc: 999.0}}

        async def fake_existing(statistic_id, start, end):
            return existing.get(statistic_id, {})

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=fake_existing
            ),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._fill_gaps(series)

        assert mock_import.call_count == 0
