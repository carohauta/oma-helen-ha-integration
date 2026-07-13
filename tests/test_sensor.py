"""Test the Helen Energy sensor platform using the real HA hass fixture."""

from unittest.mock import patch

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helen_energy.const import (
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_INCLUDE_TRANSFER_COSTS,
    CONF_VAT,
    CONTRACT_TYPE_AUTOMATIC,
    CONTRACT_TYPE_EXCHANGE,
    CONTRACT_TYPE_MARKET,
    DOMAIN,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

# ── helpers ──────────────────────────────────────────────────────────────────


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add a config entry to hass and fully set it up."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ── TestHelenDataCoordinator ──────────────────────────────────────────────────


class TestHelenDataCoordinator:
    """Test the HelenDataCoordinator."""

    async def test_coordinator_initialization(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Test that the coordinator is created and stored in hass.data after setup."""
        await _setup_entry(hass, mock_config_entry)

        assert DOMAIN in hass.data
        assert mock_config_entry.entry_id in hass.data[DOMAIN]
        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.name == "Helen Energy"
        assert coordinator.config_entry is mock_config_entry

    async def test_transfer_fee_falls_back_to_unit_price(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Transfer-only contracts use their per-kWh transfer fee as unit price."""
        mock_api_client, _ = mock_api_setup
        mock_api_client.get_contract_type.return_value = None
        mock_api_client.get_contract_energy_unit_price.return_value = 0.0
        mock_api_client.get_transfer_fee.return_value = 7.35788

        await _setup_entry(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.data["unit_price"] == 7.35788
        assert coordinator.statistics_manager._fixed_unit_price == 7.35788
        mock_api_client.get_transfer_fee.assert_called()

    async def test_energy_contract_with_zero_price_skips_transfer_fee_fallback(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Energy contracts reporting 0.0 must not pick up the transfer fee as unit price."""
        mock_api_client, _ = mock_api_setup
        mock_api_client.get_contract_type.return_value = "PERUS"
        mock_api_client.get_contract_energy_unit_price.return_value = 0.0
        mock_api_client.get_transfer_fee.return_value = 7.35788

        await _setup_entry(hass, mock_config_entry)

        coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]["coordinator"]
        assert coordinator.data["unit_price"] == 0.0
        mock_api_client.get_transfer_fee.assert_not_called()

    async def test_coordinator_network_error_preserves_data(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Network errors keep last known data rather than making entities unavailable."""
        from helenservice.api_exceptions import InvalidApiResponseException

        from custom_components.helen_energy.coordinator import HelenDataCoordinator

        await _setup_entry(hass, mock_config_entry)
        coordinator: HelenDataCoordinator = hass.data[DOMAIN][
            mock_config_entry.entry_id
        ]["coordinator"]

        # Record the state after a successful first refresh
        initial_data = dict(coordinator.data)
        assert initial_data  # must not be empty

        # Now simulate a network error on the next refresh
        with patch(
            "custom_components.helen_energy.coordinator._login_helen_api_if_needed",
            side_effect=InvalidApiResponseException("Network connection failed"),
        ):
            result = await coordinator._async_update_data()

        # Data should be preserved, not wiped
        assert result == initial_data

    async def test_last_month_consumption_zero_for_new_contract(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """When the contract started this month, last_month_consumption is 0 after catching 403 error."""
        from datetime import date
        from helenservice.api_exceptions import InvalidApiResponseException
        from unittest.mock import Mock

        mock_api_client, _ = mock_api_setup
        mock_api_client.get_contract_start_date.return_value = date.today()
        
        # Simulate API 403 error for last month (no relevant contract)
        def side_effect_for_last_month(*args, **kwargs):
            # If it's calling for last month, raise the 403 error
            from dateutil.relativedelta import relativedelta
            from helenservice.utils import get_month_date_range_by_date
            
            today_last_month = date.today() + relativedelta(months=-1)
            start_date, _ = get_month_date_range_by_date(today_last_month)
            
            if args[0] == start_date:
                raise InvalidApiResponseException(
                    'Helen chart-data request failed with status 403: '
                    '{"type":"/problems/chart-data/no-relevant-contract",'
                    '"title":"No relevant contracts for delivery site in requested period",'
                    '"status":403}'
                )
            # For other calls, return mock response with valid data
            return Mock(
                series=[
                    Mock(electricity=10.5),
                    Mock(electricity=12.3),
                    Mock(electricity=9.8),
                ]
            )

        mock_api_client.get_daily_measurements_between_dates.side_effect = (
            side_effect_for_last_month
        )

        await _setup_entry(hass, mock_config_entry)

        from custom_components.helen_energy.coordinator import HelenDataCoordinator

        coordinator: HelenDataCoordinator = hass.data[DOMAIN][
            mock_config_entry.entry_id
        ]["coordinator"]

        assert coordinator.data["last_month_consumption"] == 0.0
        # New behavior: We try to call last_month (which raises 403),
        # catch the exception, and return 0.0. So all 3 calls are attempted.
        assert mock_api_client.get_daily_measurements_between_dates.call_count == 3


# ── TestHelenFixedPriceElectricity ────────────────────────────────────────────


class TestHelenFixedPriceElectricity:
    """Test HelenFixedPriceElectricity sensor via full integration setup."""

    async def test_fixed_price_sensor_state(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Sensor state is consumption * unit_price/100 + base_price."""
        # mock series: 10.5 + 12.3 + 9.8 = 32.6 kWh
        # 32.6 * 8.5 / 100 + 5.0 = 7.77 EUR
        await _setup_entry(hass, mock_config_entry)

        state = hass.states.get("sensor.helen_fixed_price_electricity")
        assert state is not None
        assert state.state == "7.77"

    async def test_fixed_price_sensor_state_attributes(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Sensor exposes consumption and price attributes."""
        await _setup_entry(hass, mock_config_entry)

        state = hass.states.get("sensor.helen_fixed_price_electricity")
        assert state is not None
        attrs = state.attributes
        assert attrs["current_month_consumption"] == 32.6
        assert attrs["last_month_consumption"] == 32.6
        assert attrs["fixed_unit_price"] == 8.5
        assert attrs["contract_base_price"] == 5.0

    async def test_fixed_price_sensor_with_default_prices(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """Default unit/base price overrides API values."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_DEFAULT_UNIT_PRICE: 10.0,
                CONF_DEFAULT_BASE_PRICE: 3.0,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
            },
            unique_id="testuser_override",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_fixed_price_electricity")
        assert state is not None
        # 32.6 * 10.0 / 100 + 3.0 = 6.26
        assert state.state == "6.26"


# ── TestHelenMarketPriceElectricity ───────────────────────────────────────────


class TestHelenMarketPriceElectricity:
    """Test HelenMarketPriceElectricity sensor via full integration setup."""

    async def test_market_price_sensor_state(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """Sensor state is estimated current-month cost using market prices."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
                "contract_type": CONTRACT_TYPE_MARKET,
            },
            unique_id="testuser_market",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_market_price_electricity")
        assert state is not None
        # consumption=32.6, daily_avg=10.87, current_month_price=90/100=0.9
        # 5.0 + (0.9 * 32.6) + (2 * 10.87 * 0.9) = 5.0 + 29.34 + 19.57 = 53.91
        assert state.state == "53.91"

    async def test_market_price_sensor_attributes(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """Sensor exposes last/current/next month prices."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
                "contract_type": CONTRACT_TYPE_MARKET,
            },
            unique_id="testuser_market_attrs",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_market_price_electricity")
        assert state is not None
        attrs = state.attributes
        assert attrs["price_current_month"] == 90.0
        assert attrs["price_last_month"] == 85.0
        assert attrs["price_next_month"] == 88.0
        assert attrs["current_month_consumption"] == 32.6


# ── TestHelenExchangeElectricity ──────────────────────────────────────────────


class TestHelenExchangeElectricity:
    """Test HelenExchangeElectricity sensor via full integration setup."""

    async def test_exchange_sensor_state(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """Sensor state is spot-price costs + base_price for current month."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
                "contract_type": CONTRACT_TYPE_EXCHANGE,
            },
            unique_id="testuser_exchange",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_exchange_electricity")
        assert state is not None
        # exchange_costs current_month=25.5 + base_price=5.0 = 30.5
        assert state.state == "30.5"

    async def test_exchange_sensor_no_exchange_costs(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """Sensor state is unavailable when the API raises for spot price costs."""
        from helenservice.api_exceptions import InvalidApiResponseException

        mock_helen_api_client, _ = mock_api_setup
        mock_helen_api_client.calculate_total_costs_by_spot_prices_between_dates.side_effect = (
            InvalidApiResponseException("no data")
        )

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
                "contract_type": CONTRACT_TYPE_EXCHANGE,
            },
            unique_id="testuser_exchange_nodata",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_exchange_electricity")
        assert state is not None
        # exchange_costs is None → native_value is None → HA reports unknown (sensor
        # setup succeeded but has no value yet)
        assert state.state == STATE_UNKNOWN


# ── TestHelenTransferPrice ────────────────────────────────────────────────────


class TestHelenTransferPrice:
    """Test HelenTransferPrice sensor via full integration setup."""

    async def test_transfer_price_sensor_state(
        self, hass: HomeAssistant, mock_config_entry_with_transfer_costs, mock_api_setup
    ):
        """Sensor state matches the transfer fees returned by the API."""
        await _setup_entry(hass, mock_config_entry_with_transfer_costs)

        state = hass.states.get("sensor.helen_transfer_costs")
        assert state is not None
        # calculate_transfer_fees_between_dates returns 15.0
        assert state.state == "15.0"

    async def test_transfer_price_sensor_not_created_when_disabled(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Transfer cost sensor is not created when include_transfer_costs is False."""
        await _setup_entry(hass, mock_config_entry)

        state = hass.states.get("sensor.helen_transfer_costs")
        assert state is None


# ── TestHelenMonthlyConsumption ───────────────────────────────────────────────


class TestHelenMonthlyConsumption:
    """Test HelenMonthlyConsumption sensor via full integration setup."""

    async def test_monthly_consumption_sensor_state(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Sensor state is total kWh consumed in the current month."""
        await _setup_entry(hass, mock_config_entry)

        state = hass.states.get("sensor.helen_monthly_consumption")
        assert state is not None
        # series: 10.5 + 12.3 + 9.8 = 32.6 kWh
        assert state.state == "32.6"

    async def test_monthly_consumption_sensor_unit(
        self, hass: HomeAssistant, mock_config_entry, mock_api_setup
    ):
        """Sensor uses kWh as its unit of measurement."""
        await _setup_entry(hass, mock_config_entry)

        state = hass.states.get("sensor.helen_monthly_consumption")
        assert state is not None
        assert state.attributes.get("unit_of_measurement") == "kWh"


# ── TestAutomaticContractDetection ────────────────────────────────────────────


class TestAutomaticContractDetection:
    """Verify automatic contract type resolution end-to-end.

    Regression coverage: when the user picks "automatic", the coordinator must still
    fetch market_prices / exchange_costs based on the API contract type — otherwise
    the corresponding sensor crashes on missing data.
    """

    async def test_automatic_market_contract_renders(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """User on automatic + API MARK contract gets a working market sensor."""
        mock_helen_api_client, _ = mock_api_setup
        mock_helen_api_client.get_contract_type.return_value = "MARKKINASAHKO"

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
                "contract_type": CONTRACT_TYPE_AUTOMATIC,
            },
            unique_id="testuser_auto_market",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_market_price_electricity")
        assert state is not None
        assert state.state == "53.91"

    async def test_automatic_exchange_contract_renders(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """User on automatic + API PORS contract gets a working exchange sensor."""
        mock_helen_api_client, _ = mock_api_setup
        mock_helen_api_client.get_contract_type.return_value = "PORSSISAHKO"

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
                "contract_type": CONTRACT_TYPE_AUTOMATIC,
            },
            unique_id="testuser_auto_exchange",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_exchange_electricity")
        assert state is not None
        assert state.state == "30.5"


# ── Market sensor None-price resilience ───────────────────────────────────────


class TestHelenMarketPriceElectricityNoneHandling:
    """The market price API can return None fields; the sensor must not crash."""

    async def test_handles_none_price_fields(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """All price fields None → state is just the base price, no crash."""
        _, mock_price_client = mock_api_setup
        mock_price_client.get_market_price_prices.return_value.last_month = None
        mock_price_client.get_market_price_prices.return_value.current_month = None
        mock_price_client.get_market_price_prices.return_value.next_month = None

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 25.5,
                CONF_INCLUDE_TRANSFER_COSTS: False,
                CONF_DELIVERY_SITE_ID: None,
                "contract_type": CONTRACT_TYPE_MARKET,
            },
            unique_id="testuser_market_none_prices",
        )
        await _setup_entry(hass, entry)

        state = hass.states.get("sensor.helen_market_price_electricity")
        assert state is not None
        # All prices treated as 0 → only base_price (5.0) contributes
        assert state.state == "5.0"
        attrs = state.attributes
        assert attrs["price_last_month"] is None
        assert attrs["price_current_month"] is None
        assert attrs["price_next_month"] is None
