"""The Helen Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from helenservice.api_client import HelenApiClient
from helenservice.price_client import HelenPriceClient
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform

from .const import (
    CONF_DELIVERY_SITE_ID,
    CONF_INCLUDE_TRANSFER_COSTS,
    CONF_VAT,
    DOMAIN,
)
from .migration import async_migrate_entry, async_migrate_entities_for_compatibility

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Configuration schema for YAML setup (legacy support)
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_VAT, default=25.5): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=100)
                ),
                vol.Optional("contract_type", default=""): cv.string,
                vol.Optional("default_unit_price"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("default_base_price"): vol.All(
                    vol.Coerce(float), vol.Range(min=0)
                ),
                vol.Optional("delivery_site_id"): cv.string,
                vol.Optional(CONF_INCLUDE_TRANSFER_COSTS, default=False): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


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
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_USERNAME) == conf.get(CONF_USERNAME)
    ]

    if not existing_entries:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "import"}, data=conf
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Helen Energy from a config entry."""
    if entry.version != 1:
        # Perform migration if needed
        if not await async_migrate_entry(hass, entry):
            return False

    from .sensor import HelenDataCoordinator

    vat = entry.data[CONF_VAT] / 100
    delivery_site_id = entry.data.get(CONF_DELIVERY_SITE_ID)
    include_transfer_costs = entry.data.get(CONF_INCLUDE_TRANSFER_COSTS)
    credentials = {
        "username": entry.data[CONF_USERNAME],
        "password": entry.data[CONF_PASSWORD],
    }

    helen_price_client = HelenPriceClient()
    exchange_prices = await hass.async_add_executor_job(
        helen_price_client.get_exchange_prices
    )
    helen_api_client = HelenApiClient(vat, exchange_prices.margin)

    coordinator = HelenDataCoordinator(
        hass,
        entry,
        helen_api_client,
        helen_price_client,
        credentials,
        delivery_site_id,
        include_transfer_costs,
    )

    # Perform entity migration to preserve history from legacy installations.
    # Only migrate if this is the first Helen Energy entry to avoid conflicts.
    helen_entries = list(hass.config_entries.async_entries(DOMAIN))
    if len(helen_entries) == 1 and helen_entries[0] == entry:
        await async_migrate_entities_for_compatibility(hass, entry)

    # Must be called while the entry is still in SETUP_IN_PROGRESS state.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
