from datetime import date, timedelta
import logging
import math
from typing import Any

from dateutil.relativedelta import relativedelta
from helenservice.api_response import MeasurementResponse
from helenservice.api_exceptions import InvalidApiResponseException
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import (
    SCAN_INTERVAL,
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
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from .const import (
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_VAT,
    CONF_FIXED_PRICE,
    CONF_INCLUDE_TRANSFER_COSTS,
)
from helenservice.price_client import HelenPriceClient
from helenservice.api_client import HelenApiClient
from helenservice.utils import get_month_date_range_by_date

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=3)

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
        credentials: dict,
        delivery_site_id: str = None,
        include_transfer_costs: bool = False,
    ):
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
            except Exception:
                data["market_prices"] = None

            # Get exchange prices if needed
            try:
                exchange_prices = await self.hass.async_add_executor_job(
                    self.price_client.get_exchange_prices
                )
                data["exchange_prices"] = {"margin": exchange_prices.margin}
            except Exception:
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
                    "current_month": math.ceil(current_month_cost),
                    "last_month": math.ceil(last_month_cost),
                }
            except Exception:
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
            except Exception:
                data["smart_guarantee"] = None

            return data

        except InvalidApiResponseException as err:
            if "authentication" in str(err).lower():
                # Trigger reauth if it's an auth error
                from homeassistant.exceptions import ConfigEntryAuthFailed

                raise ConfigEntryAuthFailed from err
            raise UpdateFailed(f"Error communicating with Helen API: {err}")
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err
        finally:
            self.api_client.close()


async def async_setup_entry(hass, config_entry, async_add_entities):
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

    # Do first data update
    await coordinator.async_config_entry_first_refresh()

    contract_type = coordinator.data.get("contract_type")

    entities = []

    if "MARK" in contract_type and is_fixed_price is False:
        entities.append(
            HelenMarketPriceElectricity(
                coordinator,
                default_base_price,
                default_unit_price,
            )
        )
    elif "PORS" in contract_type and is_fixed_price is False:
        if default_unit_price is not None:
            _LOGGER.warning(
                "Default unit price has been set but it will not be used with EXCHANGE contract type"
            )
        entities.append(
            HelenExchangeElectricity(
                coordinator,
                default_base_price,
            )
        )
    elif "VALTTI" in contract_type and is_fixed_price is False:
        entities.append(
            HelenSmartGuarantee(
                coordinator,
                default_base_price,
                default_unit_price,
            )
        )
    elif (
        "PERUS" in contract_type or "KAYTTO" in contract_type or is_fixed_price is True
    ):
        entities.append(
            HelenFixedPriceElectricity(
                coordinator,
                default_base_price,
                default_unit_price,
            )
        )

    if include_transfer_costs is True:
        entities.append(HelenTransferPrice(coordinator))

    entities.append(HelenMonthlyConsumption(coordinator))

    async_add_entities(entities)

    return True


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
    return sum(
        m.value if m.status == "valid" else 0.0
        for m in measurement_response.intervals.electricity[0].measurements
    )


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
    return await hass.async_add_executor_job(
        helen_api_client.calculate_transfer_fees_between_dates, start_date, end_date
    )


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
    return (
        sum(valid_measurements) / len(valid_measurements) if valid_measurements else 0
    )


class HelenMarketPriceElectricity(CoordinatorEntity, Entity):
    """Helen market price electricity sensor."""

    attrs: dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_market_price_electricity"
        )
        self._attr_name = "Helen Market Price Electricity"
        self._default_base_price = default_base_price
        self._default_unit_price = default_unit_price

    @property
    def state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return self.attrs

    @property
    def state(self) -> str | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        market_prices = data.get("market_prices", {})
        contract_base_price = data.get("contract_base_price", 0)
        current_month_consumption = data.get("current_month_consumption", 0)
        daily_average_consumption = data.get("daily_average_consumption", 0)

        # Calculate current month price estimate
        current_month_price = (
            self._default_unit_price / 100
            if self._default_unit_price is not None
            else market_prices.get("current_month", 0) / 100
        )

        current_month_cost_estimate = (
            contract_base_price
            + (current_month_price * current_month_consumption)
            + (2 * daily_average_consumption * current_month_price)
        )
        return math.ceil(current_month_cost_estimate)

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        market_prices = data.get("market_prices", {})
        contract_base_price = data.get("contract_base_price", 0)
        last_month_consumption = data.get("last_month_consumption", 0)

        # Calculate last month total cost
        last_month_price = market_prices.get("last_month", 0) / 100
        last_month_total_cost = (
            last_month_price * last_month_consumption + contract_base_price
        )

        # Use default base price if set
        if self._default_base_price is not None:
            contract_base_price = self._default_base_price

        # Use default unit price for current month if set
        current_month_price = (
            self._default_unit_price
            if self._default_unit_price is not None
            else market_prices.get("current_month")
        )

        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: contract_base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: last_month_total_cost,
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: data.get("current_month_consumption"),
            STATE_ATTR_LAST_MONTH_CONSUMPTION: last_month_consumption,
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: data.get("daily_average_consumption"),
            STATE_ATTR_PRICE_LAST_MONTH: market_prices.get("last_month"),
            STATE_ATTR_PRICE_CURRENT_MONTH: current_month_price,
            STATE_ATTR_PRICE_NEXT_MONTH: market_prices.get("next_month"),
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }


class HelenExchangeElectricity(CoordinatorEntity, Entity):
    """Helen exchange electricity sensor."""

    attrs: dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_exchange_electricity"
        )
        self._attr_name = "Helen Exchange Electricity"
        self._default_base_price = default_base_price

    @property
    def state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return self.attrs

    @property
    def state(self) -> str | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        contract_base_price = data.get("contract_base_price", 0)
        if self._default_base_price is not None:
            contract_base_price = self._default_base_price

        exchange_costs = data.get("exchange_costs")
        if not exchange_costs:
            return None

        return str(exchange_costs["current_month"] + contract_base_price)

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        contract_base_price = data.get("contract_base_price", 0)
        if self._default_base_price is not None:
            contract_base_price = self._default_base_price

        exchange_costs = data.get("exchange_costs")
        if not exchange_costs:
            return {}

        last_month_total_cost = exchange_costs["last_month"] + contract_base_price

        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: contract_base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: last_month_total_cost,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: data.get("last_month_consumption"),
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: data.get("current_month_consumption"),
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: data.get("daily_average_consumption"),
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }


class HelenSmartGuarantee(CoordinatorEntity, Entity):
    """Helen smart guarantee sensor."""

    attrs: dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_smart_guarantee"
        self._attr_name = "Helen Smart Guarantee"
        self._default_base_price = default_base_price
        self._default_unit_price = default_unit_price

    @property
    def state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return self.attrs

    @property
    def state(self) -> str | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        smart_guarantee = data.get("smart_guarantee")
        if not smart_guarantee:
            return None

        contract_base_price = data.get("contract_base_price", 0)
        if self._default_base_price is not None:
            contract_base_price = self._default_base_price

        current_month_consumption = data.get("current_month_consumption", 0)
        current_month_impact = smart_guarantee["current_month_impact"]

        # Get unit price
        unit_price = data.get("unit_price", 0)
        if self._default_unit_price is not None:
            unit_price = self._default_unit_price

        current_month_energy_price_with_impact = (
            unit_price + current_month_impact
        ) / 100
        current_month_total_cost = (
            current_month_consumption * current_month_energy_price_with_impact
            + contract_base_price
        )

        return str(math.ceil(current_month_total_cost))

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        contract_base_price = data.get("contract_base_price", 0)
        if self._default_base_price is not None:
            contract_base_price = self._default_base_price

        smart_guarantee = data.get("smart_guarantee")
        if not smart_guarantee:
            return {}

        # Get unit price
        unit_price = data.get("unit_price", 0)
        if self._default_unit_price is not None:
            unit_price = self._default_unit_price

        current_month_energy_price_with_impact = (
            unit_price + smart_guarantee["current_month_impact"]
        ) / 100

        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: contract_base_price,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: data.get("last_month_consumption"),
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: data.get("current_month_consumption"),
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: data.get("daily_average_consumption"),
            STATE_ATTR_CURRENT_MONTH_PRICE_WITH_IMPACT: current_month_energy_price_with_impact,
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }


class HelenFixedPriceElectricity(CoordinatorEntity, Entity):
    """Helen fixed price electricity sensor."""

    attrs: dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}

    def __init__(
        self,
        coordinator: HelenDataCoordinator,
        default_base_price: float | None = None,
        default_unit_price: float | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_fixed_price_electricity"
        )
        self._attr_name = "Helen Fixed Price Electricity"
        self._default_base_price = default_base_price
        self._default_unit_price = default_unit_price

    @property
    def state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return self.attrs

    @property
    def state(self) -> str | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None

        data = self.coordinator.data
        contract_base_price = data.get("contract_base_price", 0)
        if self._default_base_price is not None:
            contract_base_price = self._default_base_price

        current_month_consumption = data.get("current_month_consumption", 0)
        unit_price = data.get("unit_price", 0)
        if self._default_unit_price is not None:
            unit_price = self._default_unit_price

        current_month_total_cost = (
            current_month_consumption * unit_price / 100 + contract_base_price
        )

        return math.ceil(current_month_total_cost)

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if self.coordinator.data is None:
            return {}

        data = self.coordinator.data
        contract_base_price = data.get("contract_base_price", 0)
        if self._default_base_price is not None:
            contract_base_price = self._default_base_price

        unit_price = data.get("unit_price", 0)
        if self._default_unit_price is not None:
            unit_price = self._default_unit_price

        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: contract_base_price,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: data.get("last_month_consumption"),
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: data.get("current_month_consumption"),
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: data.get("daily_average_consumption"),
            STATE_ATTR_FIXED_UNIT_PRICE: unit_price,
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
            STATE_ATTR_FIXED_UNIT_PRICE_UNIT_OF_MEASUREMENT: "c/kWh",
        }


class HelenTransferPrice(CoordinatorEntity, Entity):
    """Helen transfer price sensor."""

    attrs: dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}

    def __init__(self, coordinator: HelenDataCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_transfer_costs"
        self._attr_name = "Helen Transfer Costs"

    @property
    def state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        return self.attrs

    @property
    def state(self) -> str | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return str(self.coordinator.data.get("transfer_costs", 0))


class HelenMonthlyConsumption(CoordinatorEntity, SensorEntity):
    """Helen monthly consumption sensor."""

    def __init__(self, coordinator: HelenDataCoordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_monthly_consumption"
        )
        self._attr_name = "Helen Monthly Consumption"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_icon = "mdi:home-lightning-bolt"

    @property
    def native_value(self) -> float:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return 0
        return self.coordinator.data.get("current_month_consumption", 0)
