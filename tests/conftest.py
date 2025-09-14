"""Common test fixtures and helpers for Helen Energy integration."""

import pytest
from unittest.mock import Mock, AsyncMock
from datetime import date

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD

# Home Assistant test utilities removed since async tests were removed

from custom_components.helen_energy.const import (
    DOMAIN,
    CONF_VAT,
    CONF_FIXED_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DEFAULT_BASE_PRICE,
    CONF_INCLUDE_TRANSFER_COSTS,
    CONF_DELIVERY_SITE_ID,
)


@pytest.fixture
def mock_helen_api_client():
    """Mock Helen API client."""
    mock_client = Mock()
    mock_client.is_session_valid.return_value = True
    mock_client.login_and_init = Mock()
    mock_client.select_delivery_site_if_valid_id = Mock()
    mock_client.close = Mock()
    mock_client.get_contract_base_price.return_value = 5.0
    mock_client.get_contract_type.return_value = "PERUS"
    mock_client.get_contract_energy_unit_price.return_value = 8.5
    mock_client.get_daily_measurements_between_dates.return_value = Mock(
        intervals=Mock(
            electricity=[
                Mock(
                    measurements=[
                        Mock(value=10.5, status="valid"),
                        Mock(value=12.3, status="valid"),
                        Mock(value=9.8, status="valid"),
                    ]
                )
            ]
        )
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
    """Mock config entry."""
    return ConfigEntry(
        version=1,
        minor_version=1,
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
        source="user",
        entry_id="test_entry_id",
        unique_id="testuser_12345",
        options={},
        discovery_keys=set(),
    )


@pytest.fixture
def mock_config_entry_with_transfer_costs():
    """Mock config entry with transfer costs enabled."""
    return ConfigEntry(
        version=1,
        minor_version=1,
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
        source="user",
        entry_id="test_entry_id",
        unique_id="testuser_12345",
        options={},
        discovery_keys=set(),
    )


@pytest.fixture
def mock_coordinator_data():
    """Mock coordinator data."""
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
            "current_month": 25,
            "last_month": 23,
        },
        "smart_guarantee": {
            "current_month_impact": 1.2,
        },
    }


@pytest.fixture
def mock_hass(tmp_path):
    """Mock Home Assistant instance."""
    hass = Mock()
    hass.async_add_executor_job = AsyncMock()
    hass.config_entries = Mock()
    hass.config_entries.async_entries.return_value = []
    hass.data = {}
    hass.states = Mock()
    hass.states.get = Mock(return_value=None)
    hass.states.async_set = AsyncMock()
    hass.bus = Mock()
    hass.bus.async_fire = AsyncMock()
    hass.loop = Mock()
    hass.config = Mock()
    hass.config.time_zone = "UTC"
    hass.config.config_dir = str(tmp_path)  # Use a real path for storage
    return hass


@pytest.fixture
def mock_coordinator(mock_hass, mock_config_entry, mock_helen_api_client, mock_helen_price_client):
    """Mock Helen data coordinator."""
    from custom_components.helen_energy.sensor import HelenDataCoordinator
    
    coordinator = HelenDataCoordinator(
        mock_hass,
        mock_config_entry,
        mock_helen_api_client,
        mock_helen_price_client,
        {"username": "test", "password": "test"},
        delivery_site_id=None,
        include_transfer_costs=False,
    )
    return coordinator


class MockDateRange:
    """Mock date range utility."""
    
    @staticmethod
    def get_month_date_range_by_date(target_date: date):
        """Mock month date range."""
        return (
            date(target_date.year, target_date.month, 1),
            date(target_date.year, target_date.month, 28)
        )
