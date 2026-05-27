"""Test the Helen Energy config flow."""

from unittest.mock import Mock, patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.helen_energy.config_flow import HelenConfigFlow
from custom_components.helen_energy.const import (
    CONF_CONTRACT_TYPE,
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_INCLUDE_TRANSFER_COSTS,
    CONF_VAT,
    DOMAIN,
)


def _mock_config_flow_clients(gsrn_ids: list[str]) -> tuple[Mock, Mock]:
    """Build mocked API and price clients for driving the user config flow."""
    mock_api = Mock()
    mock_api.login_and_init = Mock()
    mock_api.get_all_gsrn_ids.return_value = gsrn_ids
    mock_api.get_contract_type.return_value = "PERUS"
    mock_api.select_delivery_site_if_valid_id = Mock()
    mock_api.close = Mock()

    mock_price = Mock()
    mock_price.get_exchange_prices.return_value = Mock(margin=0.5)
    return mock_api, mock_price


_USER_INPUT = {
    "username": "testuser",
    "password": "testpass",
    "custom_name": "Home",
    CONF_VAT: 25.5,
    CONF_CONTRACT_TYPE: "automatic",
    CONF_INCLUDE_TRANSFER_COSTS: False,
}


class TestHelenConfigFlow:
    """Test Helen Energy config flow."""

    def test_create_unique_id_and_title_with_delivery_site(self):
        """Test unique ID and title creation with delivery site."""
        flow = HelenConfigFlow()

        unique_id, title = flow._create_unique_id_and_title("testuser", "12345")

        assert unique_id == "testuser_12345"
        assert title == "Helen Energy (12345)"

    def test_create_unique_id_and_title_without_delivery_site(self):
        """Test unique ID and title creation without delivery site."""
        flow = HelenConfigFlow()

        with patch(
            "custom_components.helen_energy.config_flow.time", return_value=123456
        ):
            unique_id, title = flow._create_unique_id_and_title("testuser")

            assert unique_id == "testuser_123456"
            assert title == "Helen Energy (testuser)"

    def test_build_entry_data_minimal(self):
        """Test building entry data with minimal input."""
        flow = HelenConfigFlow()

        user_input = {
            "username": "testuser",
            "password": "testpass",
            "vat": 25.5,
        }

        data = flow._build_entry_data(user_input)

        assert data[CONF_USERNAME] == "testuser"
        assert data[CONF_PASSWORD] == "testpass"
        assert data[CONF_VAT] == 25.5
        assert len(data) == 3  # Required fields only

    def test_build_entry_data_full(self):
        """Test building entry data with all optional fields."""
        flow = HelenConfigFlow()

        user_input = {
            "username": "testuser",
            "password": "testpass",
            "vat": 25.5,
            "default_unit_price": 8.5,
            "default_base_price": 5.0,
            "delivery_site_id": "12345",
            "include_transfer_costs": True,
            "contract_type": "fixed",
        }

        data = flow._build_entry_data(user_input)

        assert data[CONF_USERNAME] == "testuser"
        assert data[CONF_PASSWORD] == "testpass"
        assert data[CONF_VAT] == 25.5
        assert data[CONF_DEFAULT_UNIT_PRICE] == 8.5
        assert data[CONF_DEFAULT_BASE_PRICE] == 5.0
        assert data[CONF_DELIVERY_SITE_ID] == "12345"
        assert data[CONF_INCLUDE_TRANSFER_COSTS]
        assert data[CONF_CONTRACT_TYPE] == "fixed"


class TestHelenSiteSelector:
    """Test the GSRN delivery-site selection step."""

    async def test_multi_site_shows_selector(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """Accounts with more than one GSRN get a selection step."""
        mock_api, mock_price = _mock_config_flow_clients(
            ["637000000000000001", "637000000000000002"]
        )

        with (
            patch(
                "custom_components.helen_energy.config_flow.HelenApiClient",
                return_value=mock_api,
            ),
            patch(
                "custom_components.helen_energy.config_flow.HelenPriceClient",
                return_value=mock_price,
            ),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input=dict(_USER_INPUT)
            )
            assert result2["type"] == FlowResultType.FORM
            assert result2["step_id"] == "select_site"

            result3 = await hass.config_entries.flow.async_configure(
                result2["flow_id"],
                user_input={CONF_DELIVERY_SITE_ID: "637000000000000002"},
            )
            await hass.async_block_till_done()

        assert result3["type"] == FlowResultType.CREATE_ENTRY
        assert result3["data"][CONF_DELIVERY_SITE_ID] == "637000000000000002"
        mock_api.select_delivery_site_if_valid_id.assert_called_once_with(
            "637000000000000002"
        )

    async def test_single_site_skips_selector(
        self, hass: HomeAssistant, mock_api_setup
    ):
        """A single-GSRN account creates the entry without a selection step."""
        mock_api, mock_price = _mock_config_flow_clients(["637000000000000001"])

        with (
            patch(
                "custom_components.helen_energy.config_flow.HelenApiClient",
                return_value=mock_api,
            ),
            patch(
                "custom_components.helen_energy.config_flow.HelenPriceClient",
                return_value=mock_price,
            ),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            result2 = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input=dict(_USER_INPUT)
            )
            await hass.async_block_till_done()

        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert CONF_DELIVERY_SITE_ID not in result2["data"]
        mock_api.select_delivery_site_if_valid_id.assert_not_called()


class TestHelenOptionsFlow:
    """Test the options flow."""

    async def test_options_flow_updates_and_reloads(
        self, hass: HomeAssistant, mock_api_setup, mock_config_entry
    ):
        """Submitting the options form stores options and reloads the entry."""
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(
            mock_config_entry.entry_id
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "init"

        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_VAT: 24.0,
                CONF_CONTRACT_TYPE: "market",
                CONF_INCLUDE_TRANSFER_COSTS: True,
                CONF_DEFAULT_UNIT_PRICE: 7.5,
            },
        )
        await hass.async_block_till_done()

        assert result2["type"] == FlowResultType.CREATE_ENTRY
        assert mock_config_entry.options[CONF_VAT] == 24.0
        assert mock_config_entry.options[CONF_CONTRACT_TYPE] == "market"
        assert mock_config_entry.options[CONF_INCLUDE_TRANSFER_COSTS] is True
        assert mock_config_entry.options[CONF_DEFAULT_UNIT_PRICE] == 7.5
