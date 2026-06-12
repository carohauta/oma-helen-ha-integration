"""Test the Helen Energy migration utilities."""

from custom_components.helen_energy.migration import (
    LEGACY_ENTITY_MAPPINGS,
    get_legacy_entity_name,
)


class TestMigrationUtilities:
    """Test migration utility functions."""

    def test_get_legacy_entity_name_known_types(self):
        """Test getting legacy entity names for known sensor types."""
        assert (
            get_legacy_entity_name("market_price_electricity")
            == "Helen Market Price Electricity"
        )
        assert (
            get_legacy_entity_name("exchange_electricity")
            == "Helen Exchange Electricity"
        )
        assert (
            get_legacy_entity_name("fixed_price_electricity")
            == "Helen Fixed Price Electricity"
        )
        assert get_legacy_entity_name("transfer_costs") == "Helen Transfer Costs"
        assert (
            get_legacy_entity_name("monthly_consumption") == "Helen Monthly Consumption"
        )

    def test_get_legacy_entity_name_unknown_type(self):
        """Test getting legacy entity name for unknown sensor type."""
        result = get_legacy_entity_name("unknown_sensor_type")
        assert result == "Helen Unknown Sensor Type"


class TestLegacyEntityMappings:
    """Test the legacy entity mappings constant."""

    def test_legacy_entity_mappings_completeness(self):
        """Test that all expected legacy entities are in the mappings."""
        expected_entities = [
            "sensor.helen_market_price_electricity",
            "sensor.helen_exchange_electricity",
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
        assert (
            LEGACY_ENTITY_MAPPINGS["sensor.helen_market_price_electricity"]
            == "market_price_electricity"
        )
        assert (
            LEGACY_ENTITY_MAPPINGS["sensor.helen_exchange_electricity"]
            == "exchange_electricity"
        )
        assert (
            LEGACY_ENTITY_MAPPINGS["sensor.helen_fixed_price_electricity"]
            == "fixed_price_electricity"
        )
        assert LEGACY_ENTITY_MAPPINGS["sensor.helen_transfer_costs"] == "transfer_costs"
        assert (
            LEGACY_ENTITY_MAPPINGS["sensor.helen_monthly_consumption"]
            == "monthly_consumption"
        )
