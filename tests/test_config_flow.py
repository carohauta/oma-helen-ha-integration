"""Test the Helen Energy config flow."""

from unittest.mock import patch

from homeassistant.const import CONF_USERNAME, CONF_PASSWORD

from custom_components.helen_energy.config_flow import HelenConfigFlow
from custom_components.helen_energy.const import (
    CONF_VAT,
    CONF_FIXED_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DEFAULT_BASE_PRICE,
    CONF_INCLUDE_TRANSFER_COSTS,
    CONF_DELIVERY_SITE_ID,
)


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
        
        with patch("custom_components.helen_energy.config_flow.time", return_value=123456):
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
        assert len(data) == 3  # Only required fields

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
            "is_fixed_price": True,
        }
        
        data = flow._build_entry_data(user_input)
        
        assert data[CONF_USERNAME] == "testuser"
        assert data[CONF_PASSWORD] == "testpass"
        assert data[CONF_VAT] == 25.5
        assert data[CONF_DEFAULT_UNIT_PRICE] == 8.5
        assert data[CONF_DEFAULT_BASE_PRICE] == 5.0
        assert data[CONF_DELIVERY_SITE_ID] == "12345"
        assert data[CONF_INCLUDE_TRANSFER_COSTS] == True
        assert data[CONF_FIXED_PRICE] == True
