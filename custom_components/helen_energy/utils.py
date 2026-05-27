"""Shared utilities for the Helen Energy integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def safe_round(value: float | None, decimals: int = 2) -> float:
    """Safely round a value, returning 0.0 if value is None or non-numeric."""
    if value is None:
        return 0.0
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return 0.0


def get_entry_position(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> tuple[bool, int]:
    """Return (is_first_entry, zero-based index) of config_entry among Helen entries."""
    entries = list(hass.config_entries.async_entries(DOMAIN))
    index = next((i for i, e in enumerate(entries) if e == config_entry), 0)
    return (bool(entries) and entries[0] == config_entry), index


def conf(config_entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read a setting, preferring options (reconfigurable) over the original data."""
    return config_entry.options.get(key, config_entry.data.get(key, default))
