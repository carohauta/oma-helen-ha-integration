"""Migration utilities for Helen Energy integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_USERNAME


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    version = config_entry.version

    # Example of how to handle a version upgrade
    if version == 1:
        # Create new data dict with migrated data
        new_data = {**config_entry.data}

        # Example: Adding a new required field with a default value
        if "include_transfer_costs" not in new_data:
            new_data["include_transfer_costs"] = False

        # Update entry
        hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            title=f"Helen Energy ({config_entry.data[CONF_USERNAME]})",
        )

        config_entry.version = 2
        version = 2

    return True
