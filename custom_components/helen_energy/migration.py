"""Migration utilities for Helen Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import CONF_USERNAME
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Legacy entity ID mappings to new ones for backward compatibility
LEGACY_ENTITY_MAPPINGS = {
    "sensor.helen_market_price_electricity": "market_price_electricity",
    "sensor.helen_exchange_electricity": "exchange_electricity",
    "sensor.helen_smart_guarantee": "smart_guarantee",
    "sensor.helen_fixed_price_electricity": "fixed_price_electricity",
    "sensor.helen_transfer_costs": "transfer_costs",
    "sensor.helen_monthly_consumption": "monthly_consumption",
}


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    version = config_entry.version
    _LOGGER.info("Migrating Helen Energy config entry from version %s", version)

    if version == 1:
        # Create new data dict with migrated data
        new_data = {**config_entry.data}

        # Add missing fields with defaults for smooth migration
        if "include_transfer_costs" not in new_data:
            new_data["include_transfer_costs"] = False

        # Update entry
        hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            title=f"Helen Energy ({config_entry.data[CONF_USERNAME]})",
        )

        config_entry.version = 2

    # Migrate entities to preserve history
    await async_migrate_entities_for_compatibility(hass, config_entry)

    return True


async def async_migrate_entities_for_compatibility(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Migrate entities to maintain backward compatibility with legacy entity IDs."""
    entity_registry = er.async_get(hass)
    migrated_count = 0

    # Get all Helen Energy config entries
    helen_entries = list(hass.config_entries.async_entries(DOMAIN))
    is_first_entry = len(helen_entries) == 1 and helen_entries[0] == config_entry

    # Get all existing Helen Energy entry IDs for orphan detection
    helen_entry_ids = {entry.entry_id for entry in helen_entries}

    # Check for existing legacy entities and migrate them to use consistent unique IDs
    for legacy_entity_id, sensor_suffix in LEGACY_ENTITY_MAPPINGS.items():
        existing_entity = entity_registry.async_get(legacy_entity_id)

        if existing_entity:
            # Only migrate if:
            # 1. This is the first Helen Energy entry AND entity has no config entry, OR
            # 2. Entity belongs to a non-existent config entry (orphaned)
            should_migrate = (
                is_first_entry and existing_entity.config_entry_id is None
            ) or (
                existing_entity.config_entry_id is not None
                and existing_entity.config_entry_id not in helen_entry_ids
            )

            if should_migrate:
                # Calculate the expected new unique ID
                new_unique_id = f"{config_entry.entry_id}_{sensor_suffix}"

                _LOGGER.info(
                    "Migrating entity %s to maintain compatibility (unique_id: %s -> %s)",
                    legacy_entity_id,
                    existing_entity.unique_id,
                    new_unique_id,
                )

                try:
                    # Update the entity registration to use our config entry and unique ID
                    entity_registry.async_update_entity(
                        existing_entity.entity_id,
                        config_entry_id=config_entry.entry_id,
                        new_unique_id=new_unique_id,
                    )
                    _LOGGER.info("Successfully migrated entity %s", legacy_entity_id)
                    migrated_count += 1

                except Exception as err:
                    _LOGGER.warning(
                        "Could not migrate entity %s: %s. Entity will be recreated with new unique ID.",
                        legacy_entity_id,
                        err,
                    )
            else:
                _LOGGER.debug(
                    "Skipping migration of %s - already belongs to a Helen Energy config entry",
                    legacy_entity_id,
                )

    if migrated_count > 0:
        _LOGGER.info(
            "Successfully migrated %d entities for Helen Energy integration",
            migrated_count,
        )
    else:
        _LOGGER.debug("No entities needed migration for Helen Energy integration")


def get_legacy_entity_name(sensor_type: str) -> str:
    """Get the legacy-compatible entity name."""
    legacy_names = {
        "market_price_electricity": "Helen Market Price Electricity",
        "exchange_electricity": "Helen Exchange Electricity",
        "smart_guarantee": "Helen Smart Guarantee",
        "fixed_price_electricity": "Helen Fixed Price Electricity",
        "transfer_costs": "Helen Transfer Costs",
        "monthly_consumption": "Helen Monthly Consumption",
    }
    return legacy_names.get(
        sensor_type, f"Helen {sensor_type.replace('_', ' ').title()}"
    )


def should_preserve_legacy_entity_id(sensor_type: str) -> bool:
    """Check if we should try to preserve the legacy entity ID."""
    return sensor_type in list(LEGACY_ENTITY_MAPPINGS.values())


def should_use_legacy_names(hass, config_entry) -> bool:
    """Determine if we should use legacy entity names for this config entry."""
    # Use legacy names only if:
    # 1. This is the first Helen Energy integration entry, AND
    # 2. There are legacy entities that can be migrated

    helen_entries = list(hass.config_entries.async_entries(DOMAIN))
    is_first_entry = len(helen_entries) == 1 and helen_entries[0] == config_entry

    if not is_first_entry:
        return False

    # Check if there are any legacy entities that could be migrated
    entity_registry = er.async_get(hass)
    for legacy_entity_id in LEGACY_ENTITY_MAPPINGS:
        existing_entity = entity_registry.async_get(legacy_entity_id)
        if existing_entity and existing_entity.config_entry_id is None:
            return True

    return False
