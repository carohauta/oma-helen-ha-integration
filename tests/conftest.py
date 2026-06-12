"""Common test fixtures and helpers for Helen Energy integration."""

from datetime import date
from unittest.mock import Mock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helen_energy.const import (
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_FIXED_PRICE,
    CONF_INCLUDE_TRANSFER_COSTS,
    CONF_VAT,
    DOMAIN,
)


@pytest.fixture
def mock_helen_api_client():
    """Mock Helen API client."""
    mock_client = Mock()
    mock_client.is_session_valid.return_value = True
    mock_client.login_and_init = Mock()
    mock_client.select_delivery_site_if_valid_id = Mock()
    mock_client.close = Mock()
    mock_client.get_contract_start_date.return_value = date(2020, 1, 1)
    mock_client.get_contract_base_price.return_value = 5.0
    mock_client.get_contract_type.return_value = "PERUS"
    mock_client.get_contract_energy_unit_price.return_value = 8.5
    mock_client.get_daily_measurements_between_dates.return_value = Mock(
        series=[
            Mock(electricity=10.5),
            Mock(electricity=12.3),
            Mock(electricity=9.8),
        ]
    )
    mock_client.calculate_transfer_fees_between_dates.return_value = 15.0
    mock_client.calculate_total_costs_by_spot_prices_between_dates.return_value = 25.5
    mock_client.calculate_impact_of_usage_between_dates.return_value = 1.2
    return mock_client


@pytest.fixture
def mock_helen_price_client():
    """Mock Helen price client."""
    mock_client = Mock()
    mock_exchange_prices = Mock()
    mock_exchange_prices.margin = 0.5
    mock_client.get_exchange_prices.return_value = mock_exchange_prices

    mock_market_prices = Mock()
    mock_market_prices.last_month = 85.0
    mock_market_prices.current_month = 90.0
    mock_market_prices.next_month = 88.0
    mock_client.get_market_price_prices.return_value = mock_market_prices

    return mock_client


@pytest.fixture
def mock_config_entry():
    """Mock config entry using MockConfigEntry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Helen Energy (testuser)",
        data={
            CONF_USERNAME: "testuser",
            CONF_PASSWORD: "testpass",
            CONF_VAT: 25.5,
            CONF_FIXED_PRICE: False,
            CONF_DEFAULT_UNIT_PRICE: None,
            CONF_DEFAULT_BASE_PRICE: None,
            CONF_INCLUDE_TRANSFER_COSTS: False,
            CONF_DELIVERY_SITE_ID: None,
        },
        unique_id="testuser_12345",
    )


@pytest.fixture
def mock_config_entry_with_transfer_costs():
    """Mock config entry with transfer costs enabled."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Helen Energy (testuser)",
        data={
            CONF_USERNAME: "testuser",
            CONF_PASSWORD: "testpass",
            CONF_VAT: 25.5,
            CONF_FIXED_PRICE: False,
            CONF_DEFAULT_UNIT_PRICE: None,
            CONF_DEFAULT_BASE_PRICE: None,
            CONF_INCLUDE_TRANSFER_COSTS: True,
            CONF_DELIVERY_SITE_ID: "12345",
        },
        unique_id="testuser_12345_transfer",
    )


@pytest.fixture
def mock_coordinator_data():
    """Mock coordinator data matching what HelenDataCoordinator._async_update_data returns."""
    return {
        "current_month_consumption": 150.5,
        "last_month_consumption": 145.2,
        "daily_average_consumption": 4.8,
        "transfer_costs": 15.0,
        "contract_base_price": 5.0,
        "contract_type": "PERUS",
        "unit_price": 8.5,
        "market_prices": {
            "last_month": 85.0,
            "current_month": 90.0,
            "next_month": 88.0,
        },
        "exchange_prices": {"margin": 0.5},
        "exchange_costs": {
            "current_month": 25.0,
            "last_month": 23.0,
        },
    }


@pytest.fixture
async def mock_api_setup(enable_custom_integrations, mock_helen_api_client, mock_helen_price_client):
    """Patch HelenApiClient and HelenPriceClient so async_setup_entry doesn't make real HTTP calls.

    Depends on enable_custom_integrations (which needs hass) so must be async.
    """
    with (
        patch(
            "custom_components.helen_energy.HelenApiClient",
            return_value=mock_helen_api_client,
        ),
        patch(
            "custom_components.helen_energy.HelenPriceClient",
            return_value=mock_helen_price_client,
        ),
        patch(
            "custom_components.helen_energy.coordinator.HelenApiClient",
            return_value=mock_helen_api_client,
        ),
        patch(
            "custom_components.helen_energy.coordinator.HelenPriceClient",
            return_value=mock_helen_price_client,
        ),
    ):
        yield mock_helen_api_client, mock_helen_price_client
