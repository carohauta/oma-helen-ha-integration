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
    CONF_VAT,
    CONF_CONTRACT_TYPE,
    CONF_INCLUDE_TRANSFER_COSTS,
    DOMAIN,
)
from helenservice.api_client import HelenApiClient
from helenservice.price_client import HelenPriceClient
from helenservice.api_exceptions import InvalidApiResponseException

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
                self.api_client = HelenApiClient(user_input[CONF_VAT], margin)

                # Test authentication
                await self.hass.async_add_executor_job(
                    self.api_client.login_and_init,
                    user_input["username"],
                    user_input["password"],
                )

                # Create unique ID from username
                unique_id = user_input["username"].lower()
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                # Create entry data
                data = {
                    CONF_USERNAME: user_input["username"],
                    CONF_PASSWORD: user_input["password"],
                    CONF_VAT: user_input["vat"],
                    CONF_CONTRACT_TYPE: user_input["contract_type"],
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

                return self.async_create_entry(
                    title=f"Helen Energy ({user_input[CONF_USERNAME]})", data=data
                )

            except InvalidApiResponseException:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            finally:
                if self.api_client:
                    self.api_client.close()

        # Define contract type options
        contract_types = {
            "MARKET": "Market price",
            "EXCHANGE": "Exchange price",
            "SMART_GUARANTEE": "Smart Guarantee",
            "FIXED": "Fixed price",
        }

        data_schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): str,
                    vol.Required("vat", default=25.5): vol.All(
                        vol.Coerce(float), vol.Range(min=0, max=100)
                    ),
                    vol.Required("contract_type"): vol.In(contract_types),
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

        placeholders = {"contract_types": ", ".join(contract_types.values())}

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=placeholders,
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
                self.api_client = HelenApiClient(entry.data[CONF_VAT], margin)

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

            except InvalidApiResponseException:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            finally:
                if self.api_client:
                    self.api_client.close()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
        )
