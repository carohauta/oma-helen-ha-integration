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

    async def test_write_statistics_chain_imports_all_three_streams(
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
            await manager._write_statistics_chain(series)

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
            await manager._write_statistics_chain(series)

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

    async def test_zero_fills_gaps_up_to_latest_real_hour(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Missing hours between walk_start and latest real hour are zero-filled; pending hours after are skipped."""
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
        last_db_hour_utc = now_hour - timedelta(hours=5)

        def make(t_utc, electricity, spot):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=electricity,
                electricity_spot_prices_vat=spot,
            )

        # +1 real, +2 missing (gap), +3 real, +4 and +5 pending (None) — should stop at +3
        series = [
            make(last_db_hour_utc + timedelta(hours=1), 1.0, 100.0),
            make(last_db_hour_utc + timedelta(hours=3), 2.0, 100.0),
            Mock(
                start=(last_db_hour_utc + timedelta(hours=4)).astimezone(helsinki_tz).isoformat(),
                stop=(last_db_hour_utc + timedelta(hours=5)).astimezone(helsinki_tz).isoformat(),
                electricity=None,
                electricity_spot_prices_vat=None,
            ),
        ]

        existing = {manager.consumption_statistic_id: {last_db_hour_utc: 100.0}}

        async def fake_existing(statistic_id, start, end):
            return existing.get(statistic_id, {})

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=fake_existing
            ),
            patch.object(manager, "_repair_zero_filled_hours"),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._write_statistics_chain(series)

        cons_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.consumption_statistic_id
        )
        cons_stats = cons_call[0][2]
        # +1: 101, +2: zero-filled (101), +3: 103 — stops here, pending +4 not written
        assert [s["sum"] for s in cons_stats] == [101.0, 101.0, 103.0]

    async def test_repair_zero_filled_hours_applies_adjustments(
        self, hass: HomeAssistant, mock_api_client
    ):
        """_repair_zero_filled_hours calls async_adjust_statistics for each zero-filled hour with real API data."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        utc = ZoneInfo("UTC")
        helsinki_tz = ZoneInfo("Europe/Helsinki")
        now_hour = datetime.now(utc).replace(minute=0, second=0, microsecond=0)
        h1 = now_hour - timedelta(hours=3)
        h2 = now_hour - timedelta(hours=2)
        h3 = now_hour - timedelta(hours=1)

        # DB: h1=100, h2=100 (zero-filled), h3=102 (real)
        existing_consumption = {h1: 100.0, h2: 100.0, h3: 102.0}

        def make(t_utc, electricity, spot):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=electricity,
                electricity_spot_prices_vat=spot,
            )

        # API now has real data for h2 (1.5 kWh @ 0.10 EUR/kWh)
        api_entries = {
            h1: make(h1, 2.0, 100.0),
            h2: make(h2, 1.5, 10.0),
            h3: make(h3, 2.0, 100.0),
        }

        mock_recorder = Mock()
        with patch(
            "custom_components.helen_energy.statistics.get_instance",
            return_value=mock_recorder,
        ):
            await manager._repair_zero_filled_hours(
                api_entries, existing_consumption, False
            )

        # Should have adjusted consumption and cost for h2
        adjust_calls = mock_recorder.async_adjust_statistics.call_args_list
        consumption_calls = [c for c in adjust_calls if c[0][0] == manager.consumption_statistic_id]
        cost_calls = [c for c in adjust_calls if c[0][0] == manager.cost_statistic_id]

        assert len(consumption_calls) == 1
        assert consumption_calls[0][0][1] == h2
        assert consumption_calls[0][0][2] == pytest.approx(1.5)

        assert len(cost_calls) == 1
        assert cost_calls[0][0][1] == h2
        assert cost_calls[0][0][2] == pytest.approx(0.15)  # 1.5 kWh * 0.10 EUR/kWh

    async def test_repair_skips_hours_without_api_data(
        self, hass: HomeAssistant, mock_api_client
    ):
        """_repair_zero_filled_hours does nothing when the API still has no data for a zero-filled hour."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        utc = ZoneInfo("UTC")
        now_hour = datetime.now(utc).replace(minute=0, second=0, microsecond=0)
        h1 = now_hour - timedelta(hours=2)
        h2 = now_hour - timedelta(hours=1)

        existing_consumption = {h1: 100.0, h2: 100.0}  # h2 zero-filled

        mock_recorder = Mock()
        with patch(
            "custom_components.helen_energy.statistics.get_instance",
            return_value=mock_recorder,
        ):
            # API still has nothing for h2
            await manager._repair_zero_filled_hours(
                {}, existing_consumption, False
            )

        mock_recorder.async_adjust_statistics.assert_not_called()

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
            await manager._write_statistics_chain(series)

        assert mock_import.call_count == 0

    async def test_rebuild_mode_anchors_before_range(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Rebuild mode ignores DB records inside the range and anchors on the record before it."""
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

        # The rebuild range starts 3 hours ago
        range_start = now_hour - timedelta(hours=3)

        # Anchor: a record 2 days before the range with cumulative = 500
        anchor_hour = range_start - timedelta(days=2)

        # Stale record inside the range (should be ignored as anchor)
        stale_in_range = range_start + timedelta(hours=1)

        def make(t_utc):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=1.0,
                electricity_spot_prices_vat=100.0,
            )

        series = [make(range_start + timedelta(hours=i)) for i in range(3)]

        def fake_existing(statistic_id, start, end):
            # Lookback window (before range): return anchor
            if end <= range_start:
                return {anchor_hour: 500.0}
            # Window inside range: return stale record (rebuild must not use this)
            return {stale_in_range: 999.0}

        async def async_fake_existing(statistic_id, start, end):
            return fake_existing(statistic_id, start, end)

        with (
            patch.object(
                manager,
                "_get_existing_statistics_in_window",
                side_effect=async_fake_existing,
            ),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._write_statistics_chain(series, rebuild=True)

        cons_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.consumption_statistic_id
        )
        cons_stats = cons_call[0][2]
        # Anchored at 500, 3 hours of 1.0 kWh each → 501, 502, 503
        assert [s["sum"] for s in cons_stats] == [501.0, 502.0, 503.0]

    async def test_rebuild_mode_starts_at_zero_when_no_anchor(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Rebuild with no prior data starts the cumulative chain at zero."""
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
        range_start = now_hour - timedelta(hours=2)

        def make(t_utc):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=2.0,
                electricity_spot_prices_vat=100.0,
            )

        series = [make(range_start + timedelta(hours=i)) for i in range(2)]

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
            await manager._write_statistics_chain(series, rebuild=True)

        cons_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.consumption_statistic_id
        )
        cons_stats = cons_call[0][2]
        assert [s["sum"] for s in cons_stats] == [2.0, 4.0]

    async def test_missing_spot_price_does_not_zero_out_consumption(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Real electricity with null spot price writes real kWh and 0.0 spot cost."""
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
        # Electricity-transfer site: valid kWh, spot price always null
        series = [
            Mock(
                start=(base_time + timedelta(hours=i)).isoformat(),
                stop=(base_time + timedelta(hours=i + 1)).isoformat(),
                electricity=1.23,
                electricity_spot_prices_vat=None,
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
            await manager._write_statistics_chain(series)

        def stream(statistic_id):
            for call in mock_import.call_args_list:
                meta = call[0][1]
                sid = meta["statistic_id"] if isinstance(meta, dict) else meta.statistic_id
                if sid == statistic_id:
                    return call[0][2]
            raise AssertionError(f"No import for {statistic_id}")

        # Consumption reflects real kWh, cumulative (safe_round -> 2 decimals)
        cons_stats = stream(manager.consumption_statistic_id)
        assert [s["sum"] for s in cons_stats] == [1.23, 2.46]

        # Spot cost stays flat at 0.0 (no spot price available)
        spot_stats = stream(manager.cost_statistic_id)
        assert [s["sum"] for s in spot_stats] == [0.0, 0.0]

        # Fixed cost is still calculated from the fixed unit price
        # (0.123 and 0.246 EUR, rounded to 2 decimals by safe_round)
        fixed_stats = stream(manager.fixed_cost_statistic_id)
        assert [s["sum"] for s in fixed_stats] == [0.12, 0.25]

    async def test_missing_electricity_is_zero_filled(
        self, hass: HomeAssistant, mock_api_client
    ):
        """Null electricity with a valid spot price is zero-filled (no consumption)."""
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
        last_db_hour_utc = now_hour - timedelta(hours=5)

        def make(t_utc, electricity, spot):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=electricity,
                electricity_spot_prices_vat=spot,
            )

        # +1 has a spot price but no electricity (zero-filled),
        # +2 has real electricity (walk stops here).
        series = [
            make(last_db_hour_utc + timedelta(hours=1), None, 5.0),
            make(last_db_hour_utc + timedelta(hours=2), 1.0, 100.0),
        ]

        existing = {manager.consumption_statistic_id: {last_db_hour_utc: 100.0}}

        async def fake_existing(statistic_id, start, end):
            return existing.get(statistic_id, {})

        with (
            patch.object(
                manager, "_get_existing_statistics_in_window", side_effect=fake_existing
            ),
            patch.object(manager, "_repair_zero_filled_hours"),
            patch(
                "custom_components.helen_energy.statistics.async_add_external_statistics"
            ) as mock_import,
        ):
            await manager._write_statistics_chain(series)

        cons_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.consumption_statistic_id
        )
        cons_stats = cons_call[0][2]
        # +1: zero-filled (100), +2: real +1.0 (101)
        assert [s["sum"] for s in cons_stats] == [100.0, 101.0]

        # Spot cost must not be derived from the missing consumption:
        # +1 contributes 0.0, +2 contributes 1.0 * 1.0 EUR/kWh
        cost_call = next(
            c
            for c in mock_import.call_args_list
            if (c[0][1]["statistic_id"] if isinstance(c[0][1], dict) else c[0][1].statistic_id)
            == manager.cost_statistic_id
        )
        cost_stats = cost_call[0][2]
        assert [s["sum"] for s in cost_stats] == [0.0, 1.0]

    async def test_repair_upgrades_hour_when_electricity_arrives_without_spot(
        self, hass: HomeAssistant, mock_api_client
    ):
        """A missing spot price must not block repairing a zero-filled hour."""
        manager = HelenStatisticsManager(
            hass,
            mock_api_client,
            "sensor.helen_monthly_consumption",
            "test_entry_12345678",
            "Helen Energy (test)",
        )

        utc = ZoneInfo("UTC")
        helsinki_tz = ZoneInfo("Europe/Helsinki")
        now_hour = datetime.now(utc).replace(minute=0, second=0, microsecond=0)
        h1 = now_hour - timedelta(hours=3)
        h2 = now_hour - timedelta(hours=2)
        h3 = now_hour - timedelta(hours=1)

        # DB: h1=100, h2=100 (zero-filled), h3=102 (real)
        existing_consumption = {h1: 100.0, h2: 100.0, h3: 102.0}

        def make(t_utc, electricity, spot):
            t_hki = t_utc.astimezone(helsinki_tz)
            return Mock(
                start=t_hki.isoformat(),
                stop=(t_hki + timedelta(hours=1)).isoformat(),
                electricity=electricity,
                electricity_spot_prices_vat=spot,
            )

        # API now has real electricity for h2 but still no spot price
        api_entries = {
            h1: make(h1, 2.0, 100.0),
            h2: make(h2, 1.5, None),
            h3: make(h3, 2.0, 100.0),
        }

        mock_recorder = Mock()
        with patch(
            "custom_components.helen_energy.statistics.get_instance",
            return_value=mock_recorder,
        ):
            await manager._repair_zero_filled_hours(
                api_entries, existing_consumption, False
            )

        adjust_calls = mock_recorder.async_adjust_statistics.call_args_list
        consumption_calls = [
            c for c in adjust_calls if c[0][0] == manager.consumption_statistic_id
        ]
        cost_calls = [c for c in adjust_calls if c[0][0] == manager.cost_statistic_id]

        # Consumption repaired for h2 despite missing spot price
        assert len(consumption_calls) == 1
        assert consumption_calls[0][0][1] == h2
        assert consumption_calls[0][0][2] == pytest.approx(1.5)

        # Spot cost adjustment is 0.0 (no spot price)
        assert len(cost_calls) == 1
        assert cost_calls[0][0][1] == h2
        assert cost_calls[0][0][2] == pytest.approx(0.0)
