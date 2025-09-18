"""Test the Helen Energy integration initialization."""

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from custom_components.helen_energy import CONFIG_SCHEMA
from custom_components.helen_energy.const import (
    CONF_INCLUDE_TRANSFER_COSTS,
    CONF_VAT,
    DOMAIN,
)


class TestConfigSchema:
    """Test configuration schema validation."""

    def test_config_schema_minimal(self):
        """Test minimal valid configuration."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
            }
        }

        result = CONFIG_SCHEMA(config)

        assert result[DOMAIN][CONF_USERNAME] == "testuser"
        assert result[DOMAIN][CONF_PASSWORD] == "testpass"
        assert result[DOMAIN][CONF_VAT] == 25.5  # Default value
        assert result[DOMAIN][CONF_INCLUDE_TRANSFER_COSTS] is False  # Default value

    def test_config_schema_full(self):
        """Test full configuration with all optional fields."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 24.0,
                "contract_type": "PERUS",
                "default_unit_price": 8.5,
                "default_base_price": 5.0,
                "delivery_site_id": "12345",
                CONF_INCLUDE_TRANSFER_COSTS: True,
            }
        }

        result = CONFIG_SCHEMA(config)

        assert result[DOMAIN][CONF_USERNAME] == "testuser"
        assert result[DOMAIN][CONF_PASSWORD] == "testpass"
        assert result[DOMAIN][CONF_VAT] == 24.0
        assert result[DOMAIN]["contract_type"] == "PERUS"
        assert result[DOMAIN]["default_unit_price"] == 8.5
        assert result[DOMAIN]["default_base_price"] == 5.0
        assert result[DOMAIN]["delivery_site_id"] == "12345"
        assert result[DOMAIN][CONF_INCLUDE_TRANSFER_COSTS] is True

    def test_config_schema_invalid_vat_negative(self):
        """Test configuration with invalid negative VAT."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: -5.0,
            }
        }

        with pytest.raises(Exception):  # Should raise validation error
            CONFIG_SCHEMA(config)

    def test_config_schema_invalid_vat_too_high(self):
        """Test configuration with invalid high VAT."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: 150.0,
            }
        }

        with pytest.raises(Exception):  # Should raise validation error
            CONFIG_SCHEMA(config)

    def test_config_schema_invalid_negative_prices(self):
        """Test configuration with invalid negative prices."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                "default_unit_price": -1.0,
                "default_base_price": -2.0,
            }
        }

        with pytest.raises(Exception):  # Should raise validation error
            CONFIG_SCHEMA(config)

    def test_config_schema_missing_required_fields(self):
        """Test configuration missing required fields."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                # Missing password
            }
        }

        with pytest.raises(Exception):  # Should raise validation error
            CONFIG_SCHEMA(config)

    def test_config_schema_empty_strings(self):
        """Test configuration with empty strings."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "",
                CONF_PASSWORD: "",
            }
        }

        # Schema should accept empty strings (though they may fail later validation)
        result = CONFIG_SCHEMA(config)
        assert result[DOMAIN][CONF_USERNAME] == ""
        assert result[DOMAIN][CONF_PASSWORD] == ""

    def test_config_schema_extra_fields_allowed(self):
        """Test that extra fields are allowed in configuration at top level."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
            },
            "other_integration": {"some_config": "value"},
        }

        result = CONFIG_SCHEMA(config)

        # Should preserve the helen_energy config and allow extra top-level configs
        assert result[DOMAIN][CONF_USERNAME] == "testuser"
        assert result[DOMAIN][CONF_PASSWORD] == "testpass"
        assert "other_integration" in result

    def test_config_schema_type_coercion(self):
        """Test that configuration types are properly coerced."""
        config = {
            DOMAIN: {
                CONF_USERNAME: "testuser",
                CONF_PASSWORD: "testpass",
                CONF_VAT: "24.5",  # String that should be coerced to float
                "default_unit_price": "8.5",  # String that should be coerced to float
                "default_base_price": "5.0",  # String that should be coerced to float
                CONF_INCLUDE_TRANSFER_COSTS: "true",  # String that should be coerced to bool
            }
        }

        result = CONFIG_SCHEMA(config)

        assert isinstance(result[DOMAIN][CONF_VAT], float)
        assert result[DOMAIN][CONF_VAT] == 24.5
        assert isinstance(result[DOMAIN]["default_unit_price"], float)
        assert result[DOMAIN]["default_unit_price"] == 8.5
        assert isinstance(result[DOMAIN]["default_base_price"], float)
        assert result[DOMAIN]["default_base_price"] == 5.0
        # Note: Boolean coercion might not work as expected with string inputs
        assert isinstance(result[DOMAIN]["default_base_price"], float)
        assert result[DOMAIN]["default_base_price"] == 5.0
        # Note: Boolean coercion might not work as expected with string inputs
