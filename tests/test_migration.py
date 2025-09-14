"""Test the Helen Energy migration utilities."""

from unittest.mock import Mock, patch


from custom_components.helen_energy.migration import (
    get_legacy_entity_name,
    should_preserve_legacy_entity_id,
    should_use_legacy_names,
    LEGACY_ENTITY_MAPPINGS,
)


class TestMigrationUtilities:
    """Test migration utility functions."""

    def test_get_legacy_entity_name_known_types(self):
        """Test getting legacy entity names for known sensor types."""
        assert get_legacy_entity_name("market_price_electricity") == "Helen Market Price Electricity"
        assert get_legacy_entity_name("exchange_electricity") == "Helen Exchange Electricity"
        assert get_legacy_entity_name("smart_guarantee") == "Helen Smart Guarantee"
        assert get_legacy_entity_name("fixed_price_electricity") == "Helen Fixed Price Electricity"
        assert get_legacy_entity_name("transfer_costs") == "Helen Transfer Costs"
        assert get_legacy_entity_name("monthly_consumption") == "Helen Monthly Consumption"

    def test_get_legacy_entity_name_unknown_type(self):
        """Test getting legacy entity name for unknown sensor type."""
        result = get_legacy_entity_name("unknown_sensor_type")
        assert result == "Helen Unknown Sensor Type"

    def test_should_preserve_legacy_entity_id_known_types(self):
        """Test should preserve legacy entity ID for known types."""
        assert should_preserve_legacy_entity_id("market_price_electricity") is True
        assert should_preserve_legacy_entity_id("exchange_electricity") is True
        assert should_preserve_legacy_entity_id("fixed_price_electricity") is True
        assert should_preserve_legacy_entity_id("transfer_costs") is True
        assert should_preserve_legacy_entity_id("monthly_consumption") is True

    def test_should_preserve_legacy_entity_id_unknown_type(self):
        """Test should preserve legacy entity ID for unknown type."""
        assert should_preserve_legacy_entity_id("unknown_type") is False

    def test_should_use_legacy_names_first_entry_with_legacy_entities(self):
        """Test should use legacy names for first entry with legacy entities."""
        mock_hass = Mock()
        mock_config_entry = Mock()
        mock_config_entry.entry_id = "test_entry"

        # Mock as first entry
        mock_hass.config_entries.async_entries.return_value = [mock_config_entry]

        # Mock entity registry with legacy entity
        mock_entity_registry = Mock()
        mock_entity_registry.async_get.return_value = Mock(config_entry_id=None)

        with patch("custom_components.helen_energy.migration.er.async_get", return_value=mock_entity_registry):
            result = should_use_legacy_names(mock_hass, mock_config_entry)
            assert result is True

    def test_should_use_legacy_names_first_entry_no_legacy_entities(self):
        """Test should use legacy names for first entry without legacy entities."""
        mock_hass = Mock()
        mock_config_entry = Mock()
        mock_config_entry.entry_id = "test_entry"

        # Mock as first entry
        mock_hass.config_entries.async_entries.return_value = [mock_config_entry]

        # Mock entity registry with no legacy entities
        mock_entity_registry = Mock()
        mock_entity_registry.async_get.return_value = None

        with patch("custom_components.helen_energy.migration.er.async_get", return_value=mock_entity_registry):
            result = should_use_legacy_names(mock_hass, mock_config_entry)
            assert result is False

    def test_should_use_legacy_names_not_first_entry(self):
        """Test should use legacy names when not first entry."""
        mock_hass = Mock()
        mock_config_entry = Mock()
        mock_config_entry.entry_id = "test_entry"

        # Mock as second entry
        mock_first_entry = Mock()
        mock_first_entry.entry_id = "first_entry"
        mock_hass.config_entries.async_entries.return_value = [mock_first_entry, mock_config_entry]

        result = should_use_legacy_names(mock_hass, mock_config_entry)
        assert result is False


class TestLegacyEntityMappings:
    """Test the legacy entity mappings constant."""

    def test_legacy_entity_mappings_completeness(self):
        """Test that all expected legacy entities are in the mappings."""
        expected_entities = [
            "sensor.helen_market_price_electricity",
            "sensor.helen_exchange_electricity",
            "sensor.helen_smart_guarantee",
            "sensor.helen_fixed_price_electricity", 
            "sensor.helen_transfer_costs",
            "sensor.helen_monthly_consumption",
        ]
        
        for entity_id in expected_entities:
            assert entity_id in LEGACY_ENTITY_MAPPINGS
            assert isinstance(LEGACY_ENTITY_MAPPINGS[entity_id], str)
            assert len(LEGACY_ENTITY_MAPPINGS[entity_id]) > 0

    def test_legacy_entity_mappings_values(self):
        """Test that legacy entity mappings have correct values."""
        assert LEGACY_ENTITY_MAPPINGS["sensor.helen_market_price_electricity"] == "market_price_electricity"
        assert LEGACY_ENTITY_MAPPINGS["sensor.helen_exchange_electricity"] == "exchange_electricity"
        assert LEGACY_ENTITY_MAPPINGS["sensor.helen_smart_guarantee"] == "smart_guarantee"
        assert LEGACY_ENTITY_MAPPINGS["sensor.helen_fixed_price_electricity"] == "fixed_price_electricity"
        assert LEGACY_ENTITY_MAPPINGS["sensor.helen_transfer_costs"] == "transfer_costs"
        assert LEGACY_ENTITY_MAPPINGS["sensor.helen_monthly_consumption"] == "monthly_consumption"
