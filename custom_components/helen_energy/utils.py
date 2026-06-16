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


def resolve_contract_type(
    user_choice: str | None, api_contract_type: str | None
) -> str:
    """Resolve the effective contract type from the user's choice and API contract code.

    The user can pick a concrete type (fixed/market/exchange) or "automatic", in which
    case the type is derived from the Helen API contract code. Falls back to fixed when
    the API code is missing or unrecognized.
    """
    from .const import (
        CONTRACT_TYPE_EXCHANGE,
        CONTRACT_TYPE_FIXED,
        CONTRACT_TYPE_MARKET,
    )

    if user_choice in (CONTRACT_TYPE_FIXED, CONTRACT_TYPE_MARKET, CONTRACT_TYPE_EXCHANGE):
        return user_choice
    if api_contract_type:
        if "PERUS" in api_contract_type or "KAYTTO" in api_contract_type:
            return CONTRACT_TYPE_FIXED
        if "MARK" in api_contract_type:
            return CONTRACT_TYPE_MARKET
        if "PORS" in api_contract_type:
            return CONTRACT_TYPE_EXCHANGE
    return CONTRACT_TYPE_FIXED
