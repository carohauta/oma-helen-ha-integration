"""Config flow for Helen Energy integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_FIXED_PRICE,
    CONF_VAT,
    CONF_INCLUDE_TRANSFER_COSTS,
    DOMAIN,
)
from helenservice.api_client import HelenApiClient
from helenservice.price_client import HelenPriceClient
from helenservice.api_exceptions import HelenAuthenticationException

_LOGGER = logging.getLogger(__name__)


class HelenConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Helen Energy."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.api_client: HelenApiClient | None = None
        self.price_client: HelenPriceClient | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                # Create API client and test connection
                self.price_client = HelenPriceClient()
                margin = await self.hass.async_add_executor_job(
                    lambda: self.price_client.get_exchange_prices().margin
                )
                self.api_client = HelenApiClient(user_input[CONF_VAT] / 100, margin)

                # Test authentication
                await self.hass.async_add_executor_job(
                    self.api_client.login_and_init,
                    user_input["username"],
                    user_input["password"],
                )

                # Create unique ID from username and delivery site ID (or timestamp)
                if "delivery_site_id" in user_input:
                    unique_id = f"{user_input['username'].lower()}_{user_input['delivery_site_id']}"
                else:
                    from time import time

                    unique_id = f"{user_input['username'].lower()}_{int(time())}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                # Create entry title
                title = "Helen Energy"
                if "delivery_site_id" in user_input:
                    title = f"{title} ({user_input['delivery_site_id']})"
                else:
                    title = f"{title} ({user_input['username']})"

                # Create entry data
                data = {
                    CONF_USERNAME: user_input["username"],
                    CONF_PASSWORD: user_input["password"],
                    CONF_VAT: user_input["vat"],
                }

                # Add optional parameters if provided
                if "default_unit_price" in user_input:
                    data[CONF_DEFAULT_UNIT_PRICE] = user_input["default_unit_price"]
                if "default_base_price" in user_input:
                    data[CONF_DEFAULT_BASE_PRICE] = user_input["default_base_price"]
                if "delivery_site_id" in user_input:
                    data[CONF_DELIVERY_SITE_ID] = user_input["delivery_site_id"]
                if "include_transfer_costs" in user_input:
                    data[CONF_INCLUDE_TRANSFER_COSTS] = user_input[
                        "include_transfer_costs"
                    ]
                if "is_fixed_price" in user_input:
                    data[CONF_FIXED_PRICE] = user_input["is_fixed_price"]

                return self.async_create_entry(title=title, data=data)

            except HelenAuthenticationException as ex:
                _LOGGER.error("Authentication failed: %s", ex)
                errors["base"] = "invalid_auth"
            except TimeoutError:
                _LOGGER.error("Connection to Helen Energy timed out")
                errors["base"] = "cannot_connect"
            except ConnectionError:
                _LOGGER.error("Failed to connect to Helen Energy")
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error while setting up Helen Energy")
                errors["base"] = "cannot_connect"
            finally:
                if self.api_client:
                    self.api_client.close()

        data_schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): str,
                    vol.Required("vat", default=25.5): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=100)
                    ),
                    vol.Optional("is_fixed_price", default=False): bool,
                    vol.Optional("default_unit_price"): vol.All(
                        vol.Coerce(float), vol.Range(min=0)
                    ),
                    vol.Optional("default_base_price"): vol.All(
                        vol.Coerce(float), vol.Range(min=0)
                    ),
                    vol.Optional("delivery_site_id"): str,
                    vol.Optional("include_transfer_costs", default=False): bool,
                }
            ),
            user_input or {},
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle reauth if token is invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Dialog that informs the user that reauth is required."""
        errors = {}

        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if entry is None:
                return self.async_abort(reason="reauth_failed")

            try:
                # Create API client and test connection
                self.price_client = HelenPriceClient()
                margin = await self.hass.async_add_executor_job(
                    lambda: self.price_client.get_exchange_prices().margin
                )
                self.api_client = HelenApiClient(entry.data[CONF_VAT] / 100, margin)

                # Test authentication
                await self.hass.async_add_executor_job(
                    self.api_client.login_and_init,
                    entry.data["username"],
                    user_input["password"],
                )

                new_data = dict(entry.data)
                new_data["password"] = user_input["password"]

                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

            except HelenAuthenticationException as ex:
                _LOGGER.error("Authentication failed during reauth: %s", ex)
                errors["base"] = "invalid_auth"
            except TimeoutError:
                _LOGGER.error("Connection to Helen Energy timed out during reauth")
                errors["base"] = "cannot_connect"
            except ConnectionError:
                _LOGGER.error("Failed to connect to Helen Energy during reauth")
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during Helen Energy reauth")
                errors["base"] = "cannot_connect"
            finally:
                if self.api_client:
                    self.api_client.close()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
        )
