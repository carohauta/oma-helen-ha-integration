"""Services for the Helen Energy integration."""

from __future__ import annotations

import logging
from datetime import date

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_BACKFILL_STATISTICS

_LOGGER = logging.getLogger(__name__)

SERVICE_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required("start_date"): cv.date,
        vol.Optional("config_entry_id"): cv.string,
    }
)

MAX_BACKFILL_DAYS = 365  # 1 year maximum


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Helen Energy integration."""

    async def handle_backfill_statistics(call: ServiceCall) -> None:
        """Handle backfill statistics service call."""
        start_date: date = call.data["start_date"]
        end_date: date = date.today()  # Always backfill to today

        # Validation
        if start_date > end_date:
            raise ServiceValidationError("start_date cannot be in the future")

        date_range = (end_date - start_date).days
        if date_range > MAX_BACKFILL_DAYS:
            raise ServiceValidationError(
                f"Date range cannot exceed {MAX_BACKFILL_DAYS} days. "
                f"Requested start_date is {date_range} days ago. "
                f"Use a more recent start_date."
            )

        config_entry_id: str | None = call.data.get("config_entry_id")
        contract_msg = (
            f" for config entry {config_entry_id}"
            if config_entry_id
            else " for all contracts"
        )
        _LOGGER.info(
            "Backfill service called: %s to %s (%d days)%s",
            start_date,
            end_date,
            date_range,
            contract_msg,
        )

        # Get all Helen Energy coordinators
        if DOMAIN not in hass.data:
            raise ServiceValidationError("Helen Energy integration not configured")

        coordinators = []
        for entry_id, entry_data in hass.data[DOMAIN].items():
            # Filter by config_entry_id if specified
            if config_entry_id and entry_id != config_entry_id:
                continue

            if isinstance(entry_data, dict) and "coordinator" in entry_data:
                coordinator = entry_data["coordinator"]
                coordinators.append(coordinator)

        if not coordinators:
            if config_entry_id:
                raise ServiceValidationError(
                    f"Config entry {config_entry_id} not found"
                )
            else:
                raise ServiceValidationError("No coordinators found")

        # Execute backfill for each coordinator
        success_count = 0
        for coordinator in coordinators:
            try:
                if coordinator.statistics_manager:
                    # Lightweight authentication check - reuses saved cookies if possible
                    if not coordinator.api_client.is_session_valid():
                        _LOGGER.debug("Session invalid, authenticating before backfill")
                        await coordinator.hass.async_add_executor_job(
                            lambda: coordinator.api_client.login_and_init(
                                coordinator.credentials["username"],
                                coordinator.credentials["password"],
                            )
                        )
                    else:
                        _LOGGER.debug("Session valid, proceeding with backfill")

                    await coordinator.statistics_manager.backfill_statistics(
                        start_date, end_date
                    )
                    success_count += 1
                    _LOGGER.info(
                        "Successfully backfilled statistics for %s",
                        coordinator.statistics_manager.entity_id,
                    )
            except ValueError as err:
                # Check if this is our custom error about contract dates
                if "outside your contract period" in str(err):
                    raise ServiceValidationError(f"Backfill failed: {err}") from err
                else:
                    raise
            except Exception as err:
                _LOGGER.error(
                    "Failed to backfill statistics for %s: %s",
                    coordinator.statistics_manager.entity_id
                    if coordinator.statistics_manager
                    else "unknown",
                    err,
                    exc_info=True,
                )

        if success_count == 0:
            raise ServiceValidationError(
                "Failed to backfill statistics for any coordinator. Check logs for details."
            )

        _LOGGER.info("Backfill completed for %d coordinator(s)", success_count)

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_STATISTICS,
        handle_backfill_statistics,
        schema=SERVICE_BACKFILL_SCHEMA,
    )

    _LOGGER.debug("Helen Energy services registered")


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload services for Helen Energy integration."""
    hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_STATISTICS)
    _LOGGER.debug("Helen Energy services unloaded")
