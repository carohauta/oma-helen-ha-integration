"""The Helen Energy integration."""

from __future__ import annotations

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant

from .const import CONF_INCLUDE_TRANSFER_COSTS, CONF_VAT, DOMAIN
from .migration import async_migrate_entry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Configuration schema for YAML setup (legacy support)
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_VAT, default=25.5): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
        vol.Optional("contract_type", default=""): cv.string,
        vol.Optional("default_unit_price"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("default_base_price"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("delivery_site_id"): cv.string,
        vol.Optional(CONF_INCLUDE_TRANSFER_COSTS, default=False): cv.boolean,
    })
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Helen Energy component from YAML."""
    if DOMAIN not in config:
        return True
        
    # Import YAML configuration into config entry
    conf = config[DOMAIN]
    _LOGGER.warning(
        "YAML configuration for Helen Energy is deprecated. "
        "The integration will automatically migrate to config entry. "
        "You can remove the YAML configuration after restarting Home Assistant."
    )
    
    # Check if we already have a config entry to avoid duplicates
    existing_entries = [
        entry for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_USERNAME) == conf.get(CONF_USERNAME)
    ]
    
    if not existing_entries:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=conf
            )
        )
    
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Helen Energy from a config entry."""
    if entry.version != 1:
        # Perform migration if needed
        if not await async_migrate_entry(hass, entry):
            return False

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
