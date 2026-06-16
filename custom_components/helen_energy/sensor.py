"""Helen Energy sensor platform."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONTRACT_TYPE,
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_FIXED_PRICE,
    CONF_INCLUDE_TRANSFER_COSTS,
    CONTRACT_TYPE_AUTOMATIC,
    CONTRACT_TYPE_EXCHANGE,
    CONTRACT_TYPE_FIXED,
    CONTRACT_TYPE_MARKET,
    DOMAIN,
)
from .coordinator import HelenDataCoordinator
from .migration import get_legacy_entity_name
from .utils import conf, get_entry_position, resolve_contract_type, safe_round

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


# common for all contract types
STATE_ATTR_DAILY_AVERAGE_CONSUMPTION = "daily_average_consumption"
STATE_ATTR_CURRENT_MONTH_CONSUMPTION = "current_month_consumption"
STATE_ATTR_LAST_MONTH_CONSUMPTION = "last_month_consumption"
STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT = "consumption_unit_of_measurement"
STATE_ATTR_CONTRACT_BASE_PRICE = "contract_base_price"

# exchange
STATE_ATTR_LAST_MONTH_PRICE_WITH_IMPACT = "last_month_price_with_impact"

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


def _assign_identity(
    entity: SensorEntity, coordinator: HelenDataCoordinator, sensor_type: str
) -> None:
    """Set unique_id and name from the config entry's position among Helen entries.

    The first entry keeps the legacy entity names/IDs for history continuity;
    additional entries get a distinguishing suffix (delivery site or sequence number).
    """
    is_first, index = get_entry_position(coordinator.hass, coordinator.config_entry)
    entry_id = coordinator.config_entry.entry_id
    if is_first:
        entity._attr_unique_id = f"{entry_id}_{sensor_type}"
        entity._attr_name = get_legacy_entity_name(sensor_type)
    else:
        entity._attr_unique_id = f"{entry_id}_{sensor_type}_{index + 1}"
        delivery_site = coordinator.config_entry.data.get(CONF_DELIVERY_SITE_ID)
        suffix = f"Site {delivery_site}" if delivery_site else str(index + 1)
        entity._attr_name = f"Helen {sensor_type.replace('_', ' ').title()} ({suffix})"


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
    coordinator: HelenDataCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        "coordinator"
    ]

    is_fixed_price = config_entry.data.get(CONF_FIXED_PRICE, False)
    default_unit_price = conf(config_entry, CONF_DEFAULT_UNIT_PRICE)
    default_base_price = conf(config_entry, CONF_DEFAULT_BASE_PRICE)
    include_transfer_costs = conf(config_entry, CONF_INCLUDE_TRANSFER_COSTS)

    # Get user's explicit contract type choice
    user_contract_type = conf(
        config_entry, CONF_CONTRACT_TYPE, CONTRACT_TYPE_AUTOMATIC
    )

    entities = []

    api_contract_type = coordinator.data.get("contract_type")
    if is_fixed_price:
        effective_contract_type = CONTRACT_TYPE_FIXED
    else:
        effective_contract_type = resolve_contract_type(
            user_contract_type, api_contract_type
        )
        # Warn when the user picked automatic and we couldn't recognise the API code
        if (
            user_contract_type == CONTRACT_TYPE_AUTOMATIC
            and effective_contract_type == CONTRACT_TYPE_FIXED
            and not (
                api_contract_type
                and ("PERUS" in api_contract_type or "KAYTTO" in api_contract_type)
            )
        ):
            _LOGGER.warning(
                "Contract type could not be determined from API (got: %s), "
                "defaulting to fixed price sensor",
                api_contract_type,
            )

    if effective_contract_type == CONTRACT_TYPE_MARKET:
        entities.append(
            HelenMarketPriceElectricity(
                coordinator, default_base_price, default_unit_price
            )
        )
    elif effective_contract_type == CONTRACT_TYPE_EXCHANGE:
        if default_unit_price is not None:
            _LOGGER.warning(
                "Default unit price set but will not be used with EXCHANGE contract"
            )
        entities.append(HelenExchangeElectricity(coordinator, default_base_price))
    else:
        entities.append(
            HelenFixedPriceElectricity(
                coordinator, default_base_price, default_unit_price
            )
        )

    # Add optional sensors
    if include_transfer_costs:
        entities.append(HelenTransferPrice(coordinator))

    entities.append(HelenMonthlyConsumption(coordinator))

    async_add_entities(entities)


class HelenBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Helen sensors."""

    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:currency-eur"

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        sensor_type: str,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        _assign_identity(self, coordinator, sensor_type)
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
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: safe_round(
                data.get("current_month_consumption", 0)
            ),
            STATE_ATTR_LAST_MONTH_CONSUMPTION: safe_round(
                data.get("last_month_consumption", 0)
            ),
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: safe_round(
                data.get("daily_average_consumption", 0)
            ),
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
            default_base_price,
            default_unit_price,
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        market_prices = data.get("market_prices") or {}
        base_price = self._get_base_price(data)
        current_month_consumption = data.get("current_month_consumption", 0)
        daily_average_consumption = data.get("daily_average_consumption", 0)

        # Calculate current month price estimate (cents → euros). Treat missing or
        # None fields as 0 so a partial API response doesn't crash the sensor.
        if self._default_unit_price is not None:
            current_month_price = self._default_unit_price / 100
        else:
            current_month_price = (market_prices.get("current_month") or 0) / 100

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
        market_prices = data.get("market_prices") or {}
        base_price = self._get_base_price(data)
        last_month_consumption = data.get("last_month_consumption", 0)

        last_month_value = market_prices.get("last_month")
        current_month_value = market_prices.get("current_month")
        next_month_value = market_prices.get("next_month")

        # Calculate last month total cost; treat missing price as 0 cents/kWh
        last_month_price = (last_month_value or 0) / 100
        last_month_total_cost = safe_round(
            last_month_price * last_month_consumption + base_price
        )

        # Use default unit price for current month if set
        current_month_price_attr = (
            self._default_unit_price
            if self._default_unit_price is not None
            else current_month_value
        )

        attributes = {
            STATE_ATTR_CONTRACT_BASE_PRICE: base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: last_month_total_cost,
            STATE_ATTR_PRICE_LAST_MONTH: safe_round(last_month_value)
            if last_month_value is not None
            else None,
            STATE_ATTR_PRICE_CURRENT_MONTH: safe_round(current_month_price_attr)
            if current_month_price_attr is not None
            else None,
            STATE_ATTR_PRICE_NEXT_MONTH: safe_round(next_month_value)
            if next_month_value is not None
            else None,
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

        current_month_cost = exchange_costs.get("current_month", 0)
        return safe_round(current_month_cost + base_price)

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

        last_month_cost = exchange_costs.get("last_month", 0)
        last_month_total_cost = safe_round(last_month_cost + base_price)

        attributes = {
            STATE_ATTR_CONTRACT_BASE_PRICE: base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: last_month_total_cost,
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
        _assign_identity(self, coordinator, "transfer_costs")

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
        _assign_identity(self, coordinator, "monthly_consumption")
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_icon = "mdi:home-lightning-bolt"

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return safe_round(self.coordinator.data.get("current_month_consumption", 0))
