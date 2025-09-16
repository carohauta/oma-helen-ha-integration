"""Helen Energy sensor platform."""

from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import Any

from dateutil.relativedelta import relativedelta
from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import InvalidApiResponseException
from helenservice.api_response import MeasurementResponse
from helenservice.price_client import HelenPriceClient
from helenservice.utils import get_month_date_range_by_date

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorStateClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_VAT,
    CONF_FIXED_PRICE,
    CONF_INCLUDE_TRANSFER_COSTS,
    DOMAIN,
)
from .migration import (
    async_migrate_entities_for_compatibility,
    get_legacy_entity_name,
    should_use_legacy_names,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=3)


def safe_round(value: float | None, decimals: int = 2) -> float:
    """Safely round a value, returning 0.0 if value is None or non-numeric."""
    if value is None:
        return 0.0
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return 0.0

# common for all contract types
STATE_ATTR_DAILY_AVERAGE_CONSUMPTION = "daily_average_consumption"
STATE_ATTR_CURRENT_MONTH_CONSUMPTION = "current_month_consumption"
STATE_ATTR_LAST_MONTH_CONSUMPTION = "last_month_consumption"
STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT = "consumption_unit_of_measurement"
STATE_ATTR_CONTRACT_BASE_PRICE = "contract_base_price"

# exchange
STATE_ATTR_LAST_MONTH_PRICE_WITH_IMPACT = "last_month_price_with_impact"
STATE_ATTR_CURRENT_MONTH_PRICE_WITH_IMPACT = "current_month_price_with_impact"

# exchange and market price
STATE_ATTR_LAST_MONTH_TOTAL_COST = "last_month_total_cost"
STATE_ATTR_CURRENT_MONTH_TOTAL_COST = "current_month_total_cost"

# market price
STATE_ATTR_PRICE_LAST_MONTH = "price_last_month"
STATE_ATTR_PRICE_CURRENT_MONTH = "price_current_month"
STATE_ATTR_PRICE_NEXT_MONTH = "price_next_month"

# fixed price
STATE_ATTR_FIXED_UNIT_PRICE = "fixed_unit_price"
STATE_ATTR_FIXED_UNIT_PRICE_UNIT_OF_MEASUREMENT = "fixed_unit_price_unit_of_measurement"


class HelenDataCoordinator(DataUpdateCoordinator):
    """Coordinator to handle Helen data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials: dict[str, str],
        delivery_site_id: str | None = None,
        include_transfer_costs: bool = False,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Helen Energy",
            update_interval=SCAN_INTERVAL,
        )
        self.config_entry = config_entry
        self.api_client = helen_api_client
        self.price_client = helen_price_client
        self.credentials = credentials
        self.delivery_site_id = delivery_site_id
        self.include_transfer_costs = include_transfer_costs

    async def _async_update_data(self):
        """Fetch data from Helen API."""
        try:
            await _login_helen_api_if_needed(
                self.hass, self.api_client, self.credentials
            )
            _select_delivery_site(self.api_client, self.delivery_site_id)

            # Get all the data we need
            data = {
                "current_month_consumption": await _get_total_consumption_for_current_month(
                    self.hass, self.api_client
                ),
                "last_month_consumption": await _get_total_consumption_for_last_month(
                    self.hass, self.api_client
                ),
                "daily_average_consumption": await _get_average_daily_consumption_for_current_month(
                    self.hass, self.api_client
                ),
                "transfer_costs": await get_transfer_price_total_for_current_month(
                    self.hass, self.api_client
                )
                if self.include_transfer_costs
                else 0.0,
                "contract_base_price": await self.hass.async_add_executor_job(
                    self.api_client.get_contract_base_price
                ),
                "contract_type": await self.hass.async_add_executor_job(
                    self.api_client.get_contract_type
                ),
            }

            # Get prices based on contract type
            try:
                data["unit_price"] = await self.hass.async_add_executor_job(
                    self.api_client.get_contract_energy_unit_price
                )
            except InvalidApiResponseException:
                data["unit_price"] = None

            # Get market prices if needed
            try:
                prices = await self.hass.async_add_executor_job(
                    self.price_client.get_market_price_prices
                )
                data["market_prices"] = {
                    "last_month": getattr(prices, "last_month", None),
                    "current_month": getattr(prices, "current_month", None),
                    "next_month": getattr(prices, "next_month", None),
                }
            except (InvalidApiResponseException, AttributeError):
                data["market_prices"] = None

            # Get exchange prices if needed
            try:
                exchange_prices = await self.hass.async_add_executor_job(
                    self.price_client.get_exchange_prices
                )
                data["exchange_prices"] = {"margin": exchange_prices.margin}
            except (InvalidApiResponseException, AttributeError):
                data["exchange_prices"] = None

            # Calculate spot price costs for exchange electricity
            try:
                current_month = date.today()
                last_month = current_month + relativedelta(months=-1)

                current_month_cost = await self.hass.async_add_executor_job(
                    self.api_client.calculate_total_costs_by_spot_prices_between_dates,
                    *get_month_date_range_by_date(current_month),
                )
                last_month_cost = await self.hass.async_add_executor_job(
                    self.api_client.calculate_total_costs_by_spot_prices_between_dates,
                    *get_month_date_range_by_date(last_month),
                )

                data["exchange_costs"] = {
                    "current_month": safe_round(current_month_cost),
                    "last_month": safe_round(last_month_cost),
                }
            except InvalidApiResponseException:
                data["exchange_costs"] = None

            # Calculate smart guarantee costs
            try:
                current_month = date.today()
                current_month_impact = await self.hass.async_add_executor_job(
                    self.api_client.calculate_impact_of_usage_between_dates,
                    *get_month_date_range_by_date(current_month),
                )
                data["smart_guarantee"] = {
                    "current_month_impact": current_month_impact,
                }
            except InvalidApiResponseException:
                data["smart_guarantee"] = None

        except InvalidApiResponseException as err:
            if "authentication" in str(err).lower():
                # Trigger reauth if it's an auth error
                raise ConfigEntryAuthFailed from err
            # For network/API errors, log the error but keep the last known data
            _LOGGER.warning(
                "Error communicating with Helen API, keeping last known values: %s", err
            )
            # Return the existing data if available, otherwise return empty dict
            return self.data if self.data is not None else {}
        except Exception as err:
            # For unexpected errors, log but don't fail the update
            _LOGGER.error("Unexpected error fetching Helen data, keeping last known values: %s", err)
            # Return the existing data if available, otherwise return empty dict
            return self.data if self.data is not None else {}
        else:
            return data
        finally:
            self.api_client.close()


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up Helen Energy sensor platform (legacy YAML support)."""
    # Suppress unused argument warnings - these are required by the platform interface
    _ = hass, config, async_add_entities, discovery_info
    
    _LOGGER.warning(
        "Platform setup for Helen Energy is deprecated and no longer supported. "
        "Please remove the 'helen_energy' platform from your sensor configuration "
        "and use the integration setup instead."
    )
    return False


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the Helen Energy sensors from a config entry."""
    username = config_entry.data[CONF_USERNAME]
    password = config_entry.data[CONF_PASSWORD]
    vat = config_entry.data[CONF_VAT] / 100
    is_fixed_price = config_entry.data.get(CONF_FIXED_PRICE, False)
    default_unit_price = config_entry.data.get(CONF_DEFAULT_UNIT_PRICE)
    default_base_price = config_entry.data.get(CONF_DEFAULT_BASE_PRICE)
    include_transfer_costs = config_entry.data.get(CONF_INCLUDE_TRANSFER_COSTS)
    delivery_site_id = config_entry.data.get(CONF_DELIVERY_SITE_ID)

    helen_price_client = HelenPriceClient()
    # Get exchange prices in executor to avoid blocking
    exchange_prices = await hass.async_add_executor_job(
        helen_price_client.get_exchange_prices
    )
    helen_api_client = HelenApiClient(vat, exchange_prices.margin)
    credentials = {"username": username, "password": password}

    coordinator = HelenDataCoordinator(
        hass,
        config_entry,
        helen_api_client,
        helen_price_client,
        credentials,
        delivery_site_id,
        include_transfer_costs,
    )

    # Perform entity migration to preserve history from legacy installations
    # Only migrate if this is the first Helen Energy entry to avoid conflicts
    helen_entries = [entry for entry in hass.config_entries.async_entries(DOMAIN)]
    if len(helen_entries) == 1 and helen_entries[0] == config_entry:
        await async_migrate_entities_for_compatibility(hass, config_entry)

    await coordinator.async_config_entry_first_refresh()

    contract_type = coordinator.data.get("contract_type")

    entities = []

    # Add appropriate price sensor based on contract type
    if is_fixed_price or "PERUS" in contract_type or "KAYTTO" in contract_type:
        entities.append(
            HelenFixedPriceElectricity(
                coordinator, default_base_price, default_unit_price
            )
        )
    elif "MARK" in contract_type:
        entities.append(
            HelenMarketPriceElectricity(
                coordinator, default_base_price, default_unit_price
            )
        )
    elif "PORS" in contract_type:
        if default_unit_price is not None:
            _LOGGER.warning(
                "Default unit price set but will not be used with EXCHANGE contract"
            )
        entities.append(HelenExchangeElectricity(coordinator, default_base_price))
    elif "VALTTI" in contract_type:
        entities.append(
            HelenSmartGuarantee(coordinator, default_base_price, default_unit_price)
        )

    # Add optional sensors
    if include_transfer_costs:
        entities.append(HelenTransferPrice(coordinator))

    entities.append(HelenMonthlyConsumption(coordinator))

    async_add_entities(entities)


async def _login_helen_api_if_needed(
    hass: HomeAssistant, helen_api_client: HelenApiClient, credentials
):
    """Login to Helen API in executor if needed."""
    if helen_api_client.is_session_valid():
        return
    helen_api_client.close()
    await hass.async_add_executor_job(
        lambda: helen_api_client.login_and_init(**credentials)
    )


def _select_delivery_site(helen_api_client: HelenApiClient, delivery_site_id):
    if delivery_site_id is not None:
        helen_api_client.select_delivery_site_if_valid_id(delivery_site_id)


async def _get_total_consumption_between_dates(
    hass: HomeAssistant,
    helen_api_client: HelenApiClient,
    start_date: date,
    end_date: date,
) -> float:
    """Get total consumption between two dates."""
    measurement_response: MeasurementResponse = await hass.async_add_executor_job(
        helen_api_client.get_daily_measurements_between_dates, start_date, end_date
    )
    if not measurement_response.intervals.electricity:
        return 0.0
    total = sum(
        m.value if m.status == "valid" else 0.0
        for m in measurement_response.intervals.electricity[0].measurements
    )
    return safe_round(total)


async def _get_total_consumption_for_last_month(
    hass: HomeAssistant, helen_api_client: HelenApiClient
):
    """Get total consumption for last month."""
    today_last_month = date.today() + relativedelta(months=-1)
    start_date, end_date = get_month_date_range_by_date(today_last_month)
    return await _get_total_consumption_between_dates(
        hass, helen_api_client, start_date, end_date
    )


async def _get_total_consumption_for_current_month(
    hass: HomeAssistant, helen_api_client: HelenApiClient
):
    """Get total consumption for current month."""
    start_date, end_date = get_month_date_range_by_date(date.today())
    return await _get_total_consumption_between_dates(
        hass, helen_api_client, start_date, end_date
    )


async def get_transfer_price_total_for_current_month(
    hass: HomeAssistant, helen_api_client: HelenApiClient
):
    """Get the total energy transfer price."""
    start_date, end_date = get_month_date_range_by_date(date.today())
    result = await hass.async_add_executor_job(
        helen_api_client.calculate_transfer_fees_between_dates, start_date, end_date
    )
    return safe_round(result)


async def _get_average_daily_consumption_for_current_month(
    hass: HomeAssistant, helen_api_client: HelenApiClient
):
    """Get average daily consumption for current month."""
    start_date, end_date = get_month_date_range_by_date(date.today())
    measurement_response: MeasurementResponse = await hass.async_add_executor_job(
        helen_api_client.get_daily_measurements_between_dates, start_date, end_date
    )
    if not measurement_response.intervals.electricity:
        return 0
    valid_measurements = [
        m.value
        for m in measurement_response.intervals.electricity[0].measurements
        if m.status == "valid"
    ]
    average = (
        sum(valid_measurements) / len(valid_measurements) if valid_measurements else 0
    )
    return safe_round(average)


class HelenBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Helen sensors."""

    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:currency-eur"

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        sensor_type: str,
        name: str | None = None,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        
        # Generate unique ID - add suffix for additional entries
        helen_entries = [entry for entry in coordinator.hass.config_entries.async_entries(DOMAIN)]
        is_first_entry = len(helen_entries) >= 1 and helen_entries[0] == coordinator.config_entry
        
        if is_first_entry:
            self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{sensor_type}"
        else:
            entry_index = next((i for i, entry in enumerate(helen_entries) if entry == coordinator.config_entry), 1)
            self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{sensor_type}_{entry_index + 1}"
        
        # For legacy compatibility, use simple names for first entry, 
        # but include distinguishing info for additional entries to avoid conflicts
        if name:
            self._attr_name = name
        else:
            # Check if this is the first Helen Energy entry for legacy compatibility
            helen_entries = [entry for entry in coordinator.hass.config_entries.async_entries(DOMAIN)]
            is_first_entry = len(helen_entries) >= 1 and helen_entries[0] == coordinator.config_entry
            
            if is_first_entry and should_use_legacy_names(coordinator.hass, coordinator.config_entry):
                # Use legacy names for true migration cases
                self._attr_name = get_legacy_entity_name(sensor_type)
            elif is_first_entry:
                # Use simple names for first entry in new installations
                self._attr_name = get_legacy_entity_name(sensor_type)
            else:
                # Use distinguishing names for additional entries
                delivery_site = coordinator.config_entry.data.get(CONF_DELIVERY_SITE_ID)
                if delivery_site:
                    suffix = f"Site {delivery_site}"
                else:
                    # Use entry sequence number (starting from 2)
                    entry_index = next((i for i, entry in enumerate(helen_entries) if entry == coordinator.config_entry), 1)
                    suffix = str(entry_index + 1)
                
                self._attr_name = f"Helen {sensor_type.replace('_', ' ').title()} ({suffix})"
        
        self._default_base_price = default_base_price
        self._default_unit_price = default_unit_price

    def _get_base_price(self, data: dict[str, Any]) -> float:
        """Get base price with override if set."""
        if self._default_base_price is not None:
            return safe_round(self._default_base_price)
        return safe_round(data.get("contract_base_price", 0))

    def _get_unit_price(self, data: dict[str, Any]) -> float:
        """Get unit price with override if set."""
        if self._default_unit_price is not None:
            return safe_round(self._default_unit_price)
        return safe_round(data.get("unit_price", 0))

    def _get_consumption_attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        """Get common consumption attributes."""
        return {
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: safe_round(data.get("current_month_consumption", 0)),
            STATE_ATTR_LAST_MONTH_CONSUMPTION: safe_round(data.get("last_month_consumption", 0)),
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: safe_round(data.get("daily_average_consumption", 0)),
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }


class HelenMarketPriceElectricity(HelenBaseSensor):
    """Helen market price electricity sensor."""

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            "market_price_electricity",
            None,  # Use legacy-compatible name
            default_base_price,
            default_unit_price,
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        market_prices = data.get("market_prices", {})
        base_price = self._get_base_price(data)
        current_month_consumption = data.get("current_month_consumption", 0)
        daily_average_consumption = data.get("daily_average_consumption", 0)

        # Calculate current month price estimate
        current_month_price = (
            self._default_unit_price / 100
            if self._default_unit_price is not None
            else market_prices.get("current_month", 0) / 100
        )

        current_month_cost_estimate = (
            base_price
            + (current_month_price * current_month_consumption)
            + (2 * daily_average_consumption * current_month_price)
        )
        return safe_round(current_month_cost_estimate)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        market_prices = data.get("market_prices", {})
        base_price = self._get_base_price(data)
        last_month_consumption = data.get("last_month_consumption", 0)

        # Calculate last month total cost
        last_month_price = market_prices.get("last_month", 0) / 100
        last_month_total_cost = safe_round(last_month_price * last_month_consumption + base_price)

        # Use default unit price for current month if set
        current_month_price = (
            self._default_unit_price
            if self._default_unit_price is not None
            else market_prices.get("current_month")
        )

        attributes = {
            STATE_ATTR_CONTRACT_BASE_PRICE: base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: last_month_total_cost,
            STATE_ATTR_PRICE_LAST_MONTH: safe_round(market_prices.get("last_month")) if market_prices.get("last_month") is not None else None,
            STATE_ATTR_PRICE_CURRENT_MONTH: safe_round(current_month_price) if current_month_price is not None else None,
            STATE_ATTR_PRICE_NEXT_MONTH: safe_round(market_prices.get("next_month")) if market_prices.get("next_month") is not None else None,
        }
        attributes.update(self._get_consumption_attributes(data))
        return attributes


class HelenExchangeElectricity(HelenBaseSensor):
    """Helen exchange electricity sensor."""

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            "exchange_electricity",
            None,  # Use legacy-compatible name
            default_base_price,
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        base_price = self._get_base_price(data)
        exchange_costs = data.get("exchange_costs")

        if not exchange_costs:
            return None

        return safe_round(exchange_costs["current_month"] + base_price)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        base_price = self._get_base_price(data)
        exchange_costs = data.get("exchange_costs")

        if not exchange_costs:
            return self._get_consumption_attributes(data)

        last_month_total_cost = safe_round(exchange_costs["last_month"] + base_price)

        attributes = {
            STATE_ATTR_CONTRACT_BASE_PRICE: base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: last_month_total_cost,
        }
        attributes.update(self._get_consumption_attributes(data))
        return attributes


class HelenSmartGuarantee(HelenBaseSensor):
    """Helen smart guarantee sensor."""

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            "smart_guarantee",
            None,  # Use legacy-compatible name
            default_base_price,
            default_unit_price,
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        smart_guarantee = data.get("smart_guarantee")
        if not smart_guarantee:
            return None

        base_price = self._get_base_price(data)
        current_month_consumption = data.get("current_month_consumption", 0)
        current_month_impact = smart_guarantee["current_month_impact"]
        unit_price = self._get_unit_price(data)

        current_month_energy_price_with_impact = (
            unit_price + current_month_impact
        ) / 100
        current_month_total_cost = (
            current_month_consumption * current_month_energy_price_with_impact
            + base_price
        )

        return safe_round(current_month_total_cost)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        base_price = self._get_base_price(data)
        smart_guarantee = data.get("smart_guarantee")

        if not smart_guarantee:
            return self._get_consumption_attributes(data)

        unit_price = self._get_unit_price(data)
        current_month_energy_price_with_impact = safe_round((
            unit_price + smart_guarantee["current_month_impact"]
        ) / 100)

        attributes = {
            STATE_ATTR_CONTRACT_BASE_PRICE: base_price,
            STATE_ATTR_CURRENT_MONTH_PRICE_WITH_IMPACT: current_month_energy_price_with_impact,
        }
        attributes.update(self._get_consumption_attributes(data))
        return attributes


class HelenFixedPriceElectricity(HelenBaseSensor):
    """Helen fixed price electricity sensor."""

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            "fixed_price_electricity",
            None,  # Use legacy-compatible name
            default_base_price,
            default_unit_price,
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        base_price = self._get_base_price(data)
        current_month_consumption = data.get("current_month_consumption", 0)
        unit_price = self._get_unit_price(data)

        current_month_total_cost = (
            current_month_consumption * unit_price / 100 + base_price
        )

        return safe_round(current_month_total_cost)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        base_price = self._get_base_price(data)
        unit_price = self._get_unit_price(data)

        attributes = {
            STATE_ATTR_CONTRACT_BASE_PRICE: base_price,
            STATE_ATTR_FIXED_UNIT_PRICE: unit_price,
            STATE_ATTR_FIXED_UNIT_PRICE_UNIT_OF_MEASUREMENT: "c/kWh",
        }
        attributes.update(self._get_consumption_attributes(data))
        return attributes


class HelenTransferPrice(CoordinatorEntity, SensorEntity):
    """Helen transfer price sensor."""

    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:currency-eur"

    def __init__(self, coordinator: HelenDataCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        
        # Generate unique ID - add suffix for additional entries
        helen_entries = [entry for entry in coordinator.hass.config_entries.async_entries(DOMAIN)]
        is_first_entry = len(helen_entries) >= 1 and helen_entries[0] == coordinator.config_entry
        
        if is_first_entry:
            self._attr_unique_id = f"{coordinator.config_entry.entry_id}_transfer_costs"
        else:
            entry_index = next((i for i, entry in enumerate(helen_entries) if entry == coordinator.config_entry), 1)
            self._attr_unique_id = f"{coordinator.config_entry.entry_id}_transfer_costs_{entry_index + 1}"
        
        # Check if this is the first Helen Energy entry for legacy compatibility
        helen_entries = [entry for entry in coordinator.hass.config_entries.async_entries(DOMAIN)]
        is_first_entry = len(helen_entries) >= 1 and helen_entries[0] == coordinator.config_entry
        
        if is_first_entry and should_use_legacy_names(coordinator.hass, coordinator.config_entry):
            # Use legacy names for true migration cases
            self._attr_name = get_legacy_entity_name("transfer_costs")
        elif is_first_entry:
            # Use simple names for first entry in new installations
            self._attr_name = get_legacy_entity_name("transfer_costs")
        else:
            # Use distinguishing names for additional entries
            delivery_site = coordinator.config_entry.data.get(CONF_DELIVERY_SITE_ID)
            if delivery_site:
                suffix = f"Site {delivery_site}"
            else:
                # Use entry sequence number (starting from 2)
                entry_index = next((i for i, entry in enumerate(helen_entries) if entry == coordinator.config_entry), 1)
                suffix = str(entry_index + 1)
            
            self._attr_name = f"Helen Transfer Costs ({suffix})"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return safe_round(self.coordinator.data.get("transfer_costs", 0))


class HelenMonthlyConsumption(CoordinatorEntity, SensorEntity):
    """Helen monthly consumption sensor."""

    def __init__(self, coordinator: HelenDataCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        
        # Generate unique ID - add suffix for additional entries
        helen_entries = [entry for entry in coordinator.hass.config_entries.async_entries(DOMAIN)]
        is_first_entry = len(helen_entries) >= 1 and helen_entries[0] == coordinator.config_entry
        
        if is_first_entry:
            self._attr_unique_id = f"{coordinator.config_entry.entry_id}_monthly_consumption"
        else:
            entry_index = next((i for i, entry in enumerate(helen_entries) if entry == coordinator.config_entry), 1)
            self._attr_unique_id = f"{coordinator.config_entry.entry_id}_monthly_consumption_{entry_index + 1}"
        
        # Check if this is the first Helen Energy entry for legacy compatibility
        helen_entries = [entry for entry in coordinator.hass.config_entries.async_entries(DOMAIN)]
        is_first_entry = len(helen_entries) >= 1 and helen_entries[0] == coordinator.config_entry
        
        if is_first_entry and should_use_legacy_names(coordinator.hass, coordinator.config_entry):
            # Use legacy names for true migration cases
            self._attr_name = get_legacy_entity_name("monthly_consumption")
        elif is_first_entry:
            # Use simple names for first entry in new installations
            self._attr_name = get_legacy_entity_name("monthly_consumption")
        else:
            # Use distinguishing names for additional entries
            delivery_site = coordinator.config_entry.data.get(CONF_DELIVERY_SITE_ID)
            if delivery_site:
                suffix = f"Site {delivery_site}"
            else:
                # Use entry sequence number (starting from 2)
                entry_index = next((i for i, entry in enumerate(helen_entries) if entry == coordinator.config_entry), 1)
                suffix = str(entry_index + 1)
            
            self._attr_name = f"Helen Monthly Consumption ({suffix})"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_icon = "mdi:home-lightning-bolt"

    @property
    def native_value(self) -> float:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return 0
        return safe_round(self.coordinator.data.get("current_month_consumption", 0))
