"""Config flow for Helen Energy integration."""

from __future__ import annotations

import logging
from time import time
from typing import Any

import voluptuous as vol
from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import (HelenAuthenticationException,
                                         InvalidDeliverySiteException)
from helenservice.price_client import HelenPriceClient
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (CONF_CONTRACT_TYPE, CONF_DEFAULT_BASE_PRICE,
                    CONF_DEFAULT_UNIT_PRICE, CONF_DELIVERY_SITE_ID,
                    CONF_INCLUDE_TRANSFER_COSTS, CONF_VAT,
                    CONTRACT_TYPE_AUTOMATIC, CONTRACT_TYPE_EXCHANGE,
                    CONTRACT_TYPE_FIXED, CONTRACT_TYPE_MARKET, DOMAIN)

_LOGGER = logging.getLogger(__name__)


class HelenConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Helen Energy."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.api_client: HelenApiClient | None = None
        self.price_client: HelenPriceClient | None = None

    def is_matching(self, other_flow: config_entries.ConfigFlow) -> bool:
        """Return True if the other flow is for the same domain."""
        return other_flow.handler == DOMAIN

    async def _create_api_client(self, vat: float) -> HelenApiClient:
        """Create and initialize API client."""
        if self.price_client is None:
            self.price_client = HelenPriceClient()

        margin = await self.hass.async_add_executor_job(
            lambda: self.price_client.get_exchange_prices().margin
        )
        return HelenApiClient(vat / 100, margin)

    async def _test_authentication(self, username: str, password: str) -> None:
        """Test authentication with Helen API."""
        if self.api_client is None:
            raise ValueError("API client not initialized")
 
        await self.hass.async_add_executor_job(
            self.api_client.login_and_init, username, password
        )

    def _create_unique_id_and_title(
        self, username: str, delivery_site_id: str | None = None
    ) -> tuple[str, str]:
        """Create unique ID and title for the config entry."""
        title = "Helen Energy"

        if delivery_site_id:
            unique_id = f"{username.lower()}_{delivery_site_id}"
            title = f"{title} ({delivery_site_id})"
        else:
            unique_id = f"{username.lower()}_{int(time())}"
            title = f"{title} ({username})"

        return unique_id, title

    def _build_entry_data(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Build config entry data from user input."""
        data = {
            CONF_USERNAME: user_input["username"],
            CONF_PASSWORD: user_input["password"],
            CONF_VAT: user_input["vat"],
        }

        # Add optional parameters if provided
        optional_fields = [
            ("default_unit_price", CONF_DEFAULT_UNIT_PRICE),
            ("default_base_price", CONF_DEFAULT_BASE_PRICE),
            ("delivery_site_id", CONF_DELIVERY_SITE_ID),
            ("include_transfer_costs", CONF_INCLUDE_TRANSFER_COSTS),
            (CONF_CONTRACT_TYPE, CONF_CONTRACT_TYPE),
        ]

        for input_key, config_key in optional_fields:
            if input_key in user_input:
                data[config_key] = user_input[input_key]

        return data

    def _handle_errors(self, exception: Exception) -> dict[str, str]:
        """Handle exceptions and return appropriate error dict."""
        if isinstance(exception, HelenAuthenticationException):
            _LOGGER.error("Authentication failed: %s", exception)
            return {"base": "invalid_auth"}

        if isinstance(exception, InvalidDeliverySiteException):
            _LOGGER.error("Setting delivery site failed: %s", exception)
            return {"base": "invalid_delivery_site_id"}

        if isinstance(exception, (TimeoutError, ConnectionError)):
            error_type = (
                "timed out" if isinstance(exception, TimeoutError) else "failed"
            )
            _LOGGER.error("Connection to Helen Energy %s", error_type)
            return {"base": "cannot_connect"}

        _LOGGER.exception("Unexpected error while setting up Helen Energy")
        return {"base": "cannot_connect"}

    async def _validate_contract_type(self) -> tuple[bool, str | None]:
        """Validate that the contract type is supported."""
        if self.api_client is None:
            raise ValueError("API client not initialized")

        try:
            contract_type = await self.hass.async_add_executor_job(
                self.api_client.get_contract_type
            )

            # If contract type is None, we cannot automatically detect it
            if contract_type is None:
                return False, None

            # Check if contract type is supported
            supported_types = ["PERUS", "KAYTTO", "MARK", "PORS", "VALTTI"]
            if any(supported_type in contract_type for supported_type in supported_types):
                return True, None

            return False, contract_type
        except Exception as ex: # pylint: disable=broad-exception-caught
            _LOGGER.warning("Could not validate contract type: %s", ex)
            # If we can't validate due to an exception, also return failure
            return False, None

    async def _cleanup_resources(self) -> None:
        """Clean up any initialized resources."""
        if self.price_client is not None:
            # PriceClient doesn't have async close method, just clear reference
            self.price_client = None

        if self.api_client is not None:
            # Close the underlying session before clearing reference
            await self.hass.async_add_executor_job(self.api_client.close)
            self.api_client = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                # Create API client and test authentication
                self.api_client = await self._create_api_client(user_input[CONF_VAT])
                await self._test_authentication(
                    user_input["username"], user_input["password"]
                )

                # Validate delivery site if provided
                if "delivery_site_id" in user_input:
                    await self.hass.async_add_executor_job(
                        self.api_client.select_delivery_site_if_valid_id,
                        user_input["delivery_site_id"],
                    )

                # Validate contract type only when AUTOMATIC is selected
                if user_input.get(CONF_CONTRACT_TYPE) == CONTRACT_TYPE_AUTOMATIC:
                    is_supported, contract_type = await self._validate_contract_type()
                    if not is_supported:
                        await self._cleanup_resources()
                        error_key = ("contract_type_not_detected" if contract_type is None
                                    else "contract_type_not_resolved")
                        return self.async_show_form(
                            step_id="user",
                            data_schema=self._get_user_schema(user_input),
                            errors={"base": error_key},
                            description_placeholders={"contract_type": contract_type or "None"}
                        )

                # Create unique ID and title
                unique_id, title = self._create_unique_id_and_title(
                    user_input["username"], user_input.get("delivery_site_id")
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                # Build entry data and create entry
                data = self._build_entry_data(user_input)
                await self._cleanup_resources()
                return self.async_create_entry(title=title, data=data)

            except (
                HelenAuthenticationException,
                InvalidDeliverySiteException,
                TimeoutError,
                ConnectionError,
                ValueError,
            ) as ex:
                errors = self._handle_errors(ex)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during Helen Energy setup")
                errors = {"base": "cannot_connect"}
            finally:
                await self._cleanup_resources()

        return self.async_show_form(
            step_id="user", data_schema=self._get_user_schema(user_input), errors=errors
        )

    def _get_user_schema(self, user_input: dict[str, Any] | None = None) -> vol.Schema:
        """Get the user input schema."""
        return self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required("username"): str,
                    vol.Required("password"): str,
                    vol.Required("vat", default=25.5): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=0.0, max=100.0),
                        msg="VAT percentage must be between 0 and 100",
                    ),
                    vol.Required(
                        CONF_CONTRACT_TYPE, default=CONTRACT_TYPE_AUTOMATIC
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=CONTRACT_TYPE_AUTOMATIC, label="automatic"
                                ),
                                selector.SelectOptionDict(
                                    value=CONTRACT_TYPE_FIXED, label="fixed"
                                ),
                                selector.SelectOptionDict(
                                    value=CONTRACT_TYPE_MARKET, label="market"
                                ),
                                selector.SelectOptionDict(
                                    value=CONTRACT_TYPE_EXCHANGE, label="exchange"
                                ),
                            ],
                            mode="list",
                            translation_key="contract_type",
                        )
                    ),
                    vol.Optional("delivery_site_id"): str,
                    vol.Optional("default_unit_price"): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=0.00001),
                        msg="Unit price must be greater than 0",
                    ),
                    vol.Optional("default_base_price"): vol.All(
                        vol.Coerce(float),
                        vol.Range(min=0.00001),
                        msg="Base price must be greater than 0",
                    ),
                    vol.Optional("include_transfer_costs", default=False): bool,
                }
            ),
            user_input or {},
        )

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> FlowResult:
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
                # Create API client and test authentication
                self.api_client = await self._create_api_client(entry.data[CONF_VAT])
                await self._test_authentication(
                    entry.data[CONF_USERNAME], user_input["password"]
                )

                # Update entry with new password
                new_data = dict(entry.data)
                new_data[CONF_PASSWORD] = user_input["password"]

                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                await self._cleanup_resources()
                return self.async_abort(reason="reauth_successful")

            except (
                HelenAuthenticationException,
                TimeoutError,
                ConnectionError,
                ValueError,
            ) as ex:
                errors = self._handle_errors(ex)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during Helen Energy reauth")
                errors = {"base": "cannot_connect"}
            finally:
                await self._cleanup_resources()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            errors=errors,
        )

    async def async_step_import(self, user_input: dict[str, Any]) -> FlowResult:
        """Import configuration from YAML."""
        _LOGGER.info("Importing Helen Energy configuration from YAML")

        # Create unique ID from username
        unique_id = f"{user_input[CONF_USERNAME].lower()}_yaml_import"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        try:
            # Validate credentials by creating API client and testing login
            self.api_client = await self._create_api_client(user_input.get(CONF_VAT, 25.5))
            await self._test_authentication(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )

            # Map legacy contract_type to our new contract type selection
            legacy_contract_type = user_input.get("contract_type", "").upper()
            if legacy_contract_type == "FIXED":
                contract_type = CONTRACT_TYPE_FIXED
            elif legacy_contract_type == "MARKET":
                contract_type = CONTRACT_TYPE_MARKET
            elif legacy_contract_type == "EXCHANGE":
                contract_type = CONTRACT_TYPE_EXCHANGE
            else:
                contract_type = CONTRACT_TYPE_AUTOMATIC

            # Create config entry data
            data = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_VAT: user_input.get(CONF_VAT, 25.5),
                CONF_CONTRACT_TYPE: contract_type,
                CONF_INCLUDE_TRANSFER_COSTS: user_input.get(CONF_INCLUDE_TRANSFER_COSTS, False),
            }

            # Add optional fields if present
            if "default_unit_price" in user_input:
                data[CONF_DEFAULT_UNIT_PRICE] = user_input["default_unit_price"]
            if "default_base_price" in user_input:
                data[CONF_DEFAULT_BASE_PRICE] = user_input["default_base_price"]
            if "delivery_site_id" in user_input:
                data[CONF_DELIVERY_SITE_ID] = user_input["delivery_site_id"]

            return self.async_create_entry(
                title=f"Helen Energy ({user_input[CONF_USERNAME]})",
                data=data
            )
        except HelenAuthenticationException:
            _LOGGER.error("Authentication failed during YAML import")
            return self.async_abort(reason="invalid_auth")
        except Exception as err: # pylint: disable=broad-exception-caught
            _LOGGER.error("Unexpected error during YAML import: %s", err)
            return self.async_abort(reason="unknown")
        finally:
            await self._cleanup_resources()
