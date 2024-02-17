from datetime import date, datetime, timedelta
import logging
import math
from typing import Any, Dict, Optional

from dateutil.relativedelta import relativedelta
from helenservice.api_response import MeasurementResponse
from helenservice.api_exceptions import InvalidApiResponseException
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SCAN_INTERVAL,
    SensorDeviceClass,
    SensorStateClass,
    SensorEntity,
)
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    STATE_UNAVAILABLE,
    UnitOfEnergy,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
)
import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA
from .const import (
    CONF_DEFAULT_BASE_PRICE,
    CONF_DEFAULT_UNIT_PRICE,
    CONF_DELIVERY_SITE_ID,
    CONF_VAT,
    CONF_CONTRACT_TYPE,
    CONF_INCLUDE_TRANSFER_COSTS,
)
from helenservice.price_client import HelenPriceClient
from helenservice.api_client import HelenApiClient
from helenservice.utils import get_month_date_range_by_date

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=3)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_VAT): cv.positive_float,
        vol.Required(CONF_CONTRACT_TYPE): cv.string,
        vol.Optional(CONF_DEFAULT_UNIT_PRICE): cv.positive_float,
        vol.Optional(CONF_DEFAULT_BASE_PRICE): cv.positive_float,
        vol.Optional(CONF_INCLUDE_TRANSFER_COSTS): cv.boolean,
        vol.Optional(CONF_DELIVERY_SITE_ID): cv.string,
    }
)

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


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType = None,
) -> None:
    """Set up the Helen Energy platform."""
    username = config[CONF_USERNAME]
    vat = config[CONF_VAT]
    contract_type = config[CONF_CONTRACT_TYPE]
    password = config.get(CONF_PASSWORD)
    default_unit_price = config.get(CONF_DEFAULT_UNIT_PRICE)
    default_base_price = config.get(CONF_DEFAULT_BASE_PRICE)
    include_transfer_costs = config.get(CONF_INCLUDE_TRANSFER_COSTS)
    delivery_site_id = config.get(CONF_DELIVERY_SITE_ID)

    helen_price_client = HelenPriceClient()

    # initial margin
    margin = helen_price_client.get_exchange_prices().margin
    helen_api_client = HelenApiClient(vat, margin)

    credentials = {"username": username, "password": password}

    entities = []

    if contract_type == "MARKET":
        entities.append(
            HelenMarketPriceElectricity(
                helen_api_client,
                helen_price_client,
                credentials,
                default_base_price,
                default_unit_price,
                delivery_site_id,
            )
        )
    elif contract_type == "EXCHANGE":
        if default_unit_price is not None:
            _LOGGER.warn(
                "Default unit price has been set but it will not be used with EXCHANGE contract type."
            )
        entities.append(
            HelenExchangeElectricity(
                helen_api_client,
                helen_price_client,
                credentials,
                default_base_price,
                delivery_site_id,
            )
        )
    elif contract_type == "SMART_GUARANTEE":
        entities.append(
            HelenSmartGuarantee(
                helen_api_client,
                helen_price_client,
                credentials,
                default_base_price,
                default_unit_price,
                delivery_site_id,
            )
        )
    elif contract_type == "FIXED":
        entities.append(
            HelenFixedPriceElectricity(
                helen_api_client,
                helen_price_client,
                credentials,
                default_base_price,
                default_unit_price,
                delivery_site_id,
            )
        )

    if include_transfer_costs == True:
        entities.append(
            HelenTransferPrice(helen_api_client, credentials, delivery_site_id)
        )

    entities.append(
        HelenMonthlyConsumption(helen_api_client, credentials, delivery_site_id)
    )

    add_entities(
        entities,
        True,
    )


def _login_helen_api_if_needed(helen_api_client: HelenApiClient, credentials):
    if helen_api_client.is_session_valid():
        return
    helen_api_client.close()
    helen_api_client.login_and_init(**credentials)


def _select_delivery_site(helen_api_client: HelenApiClient, delivery_site_id):
    if delivery_site_id is not None:
        helen_api_client.select_delivery_site_if_valid_id(delivery_site_id)


def _get_total_consumption_between_dates(
    helen_api_client: HelenApiClient, start_date: date, end_date: date
):
    measurement_response: MeasurementResponse = (
        helen_api_client.get_daily_measurements_between_dates(start_date, end_date)
    )
    if not measurement_response.intervals.electricity:
        return 0.0
    total = sum(
        list(
            map(
                lambda m: m.value if m.status == "valid" else 0.0,
                measurement_response.intervals.electricity[0].measurements,
            )
        )
    )
    return total


def _get_total_consumption_for_last_month(helen_api_client):
    """Total consumption for last month"""
    today_last_month = date.today() + relativedelta(months=-1)
    start_date, end_date = get_month_date_range_by_date(today_last_month)
    return _get_total_consumption_between_dates(helen_api_client, start_date, end_date)


def _get_total_consumption_for_current_month(helen_api_client):
    """Total consumption for current month"""
    start_date, end_date = get_month_date_range_by_date(date.today())
    return _get_total_consumption_between_dates(helen_api_client, start_date, end_date)


def get_transfer_price_total_for_current_month(helen_api_client: HelenApiClient):
    """Get the total energy transfer price"""
    start_date, end_date = get_month_date_range_by_date(date.today())
    return helen_api_client.calculate_transfer_fees_between_dates(start_date, end_date)


def _get_average_daily_consumption_for_current_month(helen_api_client: HelenApiClient):
    """Average daily consumption for current month"""
    start_date, end_date = get_month_date_range_by_date(date.today())
    measurement_response: MeasurementResponse = (
        helen_api_client.get_daily_measurements_between_dates(start_date, end_date)
    )
    if not measurement_response.intervals.electricity:
        return 0
    valid_measurements = list(
        map(
            lambda m: m.value,
            filter(
                lambda m: m.status == "valid",
                measurement_response.intervals.electricity[0].measurements,
            ),
        )
    )
    measurements_length = len(valid_measurements)
    if measurements_length == 0:
        return 0
    daily_average = sum(valid_measurements) / measurements_length
    return daily_average


class HelenMarketPriceElectricity(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}
    _contract_base_price = None
    _prices = None
    _last_month_total_cost = None
    _last_month_consumption = None
    _current_month_consumption = None
    _average_daily_consumption = None
    _price_last_month = None
    _price_current_month = None
    _price_next_month = None
    _latest_base_price = None
    _delivery_site_id = None

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials,
        default_base_price,
        default_unit_price,
        delivery_site_id,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "helen_market_price_electricity"
        self._name = "Helen Market Price Electricity"
        self._api_client = helen_api_client
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE
        self._default_base_price = default_base_price
        self._default_unit_price = default_unit_price
        self._delivery_site_id = delivery_site_id

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self.id

    @property
    def state_attributes(self) -> Dict[str, Any]:
        return self.attrs

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the measurement."""
        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: self._contract_base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: self._last_month_total_cost,
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: self._current_month_consumption,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: self._last_month_consumption,
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: self._average_daily_consumption,
            STATE_ATTR_PRICE_LAST_MONTH: self._price_last_month,
            STATE_ATTR_PRICE_CURRENT_MONTH: self._price_current_month,
            STATE_ATTR_PRICE_NEXT_MONTH: self._price_next_month,
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }

    def _calculate_last_month_price(self):
        last_month_price = getattr(self._prices, "last_month") / 100
        last_month_consumption = _get_total_consumption_for_last_month(self._api_client)
        last_month_cost = (
            last_month_price * last_month_consumption + self._contract_base_price
        )
        return last_month_cost

    def _calculate_current_month_price_estimate(self):
        current_month_price = (
            self._default_unit_price / 100
            if self._default_unit_price is not None
            else getattr(self._prices, "current_month") / 100
        )
        current_month_consumption = _get_total_consumption_for_current_month(
            self._api_client
        )
        current_month_daily_average_consumption = (
            _get_average_daily_consumption_for_current_month(self._api_client)
        )
        current_month_cost_estimate = (
            self._contract_base_price
            + (current_month_price * current_month_consumption)
            + (2 * current_month_daily_average_consumption * current_month_price)
        )
        return math.ceil(current_month_cost_estimate)

    def update(self):
        _login_helen_api_if_needed(self._api_client, self.credentials)
        _select_delivery_site(self._api_client, self._delivery_site_id)
        self._prices = self._price_client.get_market_price_prices()
        self._price_last_month = getattr(self._prices, "last_month")
        self._price_current_month = (
            self._default_unit_price
            if self._default_unit_price is not None
            else getattr(self._prices, "current_month")
        )
        self._price_next_month = getattr(self._prices, "next_month")
        try:
            fetched_base_price = self._api_client.get_contract_base_price()
            self._contract_base_price = fetched_base_price
            self._latest_base_price = fetched_base_price  # save the latest value
        except InvalidApiResponseException:
            _LOGGER.error(
                "Received invalid response from Helen API when fetching contract base price - using the latest value if it exists, or 0 if it doesn't"
            )
            self._contract_base_price = (
                self._latest_base_price if self._latest_base_price is not None else 0
            )

        if self._default_base_price is not None:
            _LOGGER.info(f"Using the default base price: {self._default_base_price}")
            self._contract_base_price = self._default_base_price

        self._state = self._calculate_current_month_price_estimate()
        self._last_month_total_cost = self._calculate_last_month_price()
        self._average_daily_consumption = (
            _get_average_daily_consumption_for_current_month(self._api_client)
        )
        self._current_month_consumption = _get_total_consumption_for_current_month(
            self._api_client
        )
        self._last_month_consumption = _get_total_consumption_for_last_month(
            self._api_client
        )
        self._api_client.close()


class HelenExchangeElectricity(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}
    _contract_base_price = None
    _last_month_total_cost = None
    _last_month_consumption = None
    _current_month_consumption = None
    _average_daily_consumption = None
    _latest_base_price = None
    _delivery_site_id = None

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials,
        default_base_price,
        delivery_site_id,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "helen_exchange_electricity"
        self._name = "Helen Exchange Electricity"
        self._api_client = helen_api_client
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE
        self._default_base_price = default_base_price
        self._delivery_site_id = delivery_site_id

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self.id

    @property
    def state_attributes(self) -> Dict[str, Any]:
        return self.attrs

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the measurement."""
        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: self._contract_base_price,
            STATE_ATTR_LAST_MONTH_TOTAL_COST: self._last_month_total_cost,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: self._last_month_consumption,
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: self._current_month_consumption,
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: self._average_daily_consumption,
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }

    def update(self):
        _login_helen_api_if_needed(self._api_client, self.credentials)
        _select_delivery_site(self._api_client, self._delivery_site_id)
        margin = self._price_client.get_exchange_prices().margin
        self._api_client.set_margin(margin)
        current_month = date.today()
        last_month = date.today() + relativedelta(months=-1)
        current_month_total_cost = math.ceil(
            self._api_client.calculate_total_costs_by_spot_prices_between_dates(
                *get_month_date_range_by_date(current_month)
            )
        )
        last_month_total_cost = math.ceil(
            self._api_client.calculate_total_costs_by_spot_prices_between_dates(
                *get_month_date_range_by_date(last_month)
            )
        )

        try:
            fetched_base_price = self._api_client.get_contract_base_price()
            self._contract_base_price = fetched_base_price
            self._latest_base_price = fetched_base_price  # save the latest value
        except InvalidApiResponseException:
            _LOGGER.error(
                "Received invalid response from Helen API when fetching contract base price - using the latest value if it exists, or 0 if it doesn't"
            )
            self._contract_base_price = (
                self._latest_base_price if self._latest_base_price is not None else 0
            )

        if self._default_base_price is not None:
            _LOGGER.info(f"Using the default base price: {self._default_base_price}")
            self._contract_base_price = self._default_base_price

        self._state = current_month_total_cost + self._contract_base_price
        self._last_month_total_cost = last_month_total_cost + self._contract_base_price
        self._average_daily_consumption = math.ceil(
            _get_average_daily_consumption_for_current_month(self._api_client)
        )
        self._current_month_consumption = math.ceil(
            _get_total_consumption_for_current_month(self._api_client)
        )
        self._last_month_consumption = math.ceil(
            _get_total_consumption_for_last_month(self._api_client)
        )
        self._api_client.close()


class HelenSmartGuarantee(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}
    _contract_base_price = None
    _last_month_consumption = None
    _current_month_consumption = None
    _current_month_energy_price_with_impact = None
    _average_daily_consumption = None
    _latest_base_price = None
    _latest_unit_price = None
    _delivery_site_id = None

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials,
        default_base_price,
        default_unit_price,
        delivery_site_id,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "helen_smart_guarantee"
        self._name = "Helen Smart Guarantee"
        self._api_client = helen_api_client
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE
        self._default_base_price = default_base_price
        self._default_unit_price = default_unit_price
        self._delivery_site_id = delivery_site_id

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self.id

    @property
    def state_attributes(self) -> Dict[str, Any]:
        return self.attrs

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the measurement."""
        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: self._contract_base_price,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: self._last_month_consumption,
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: self._current_month_consumption,
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: self._average_daily_consumption,
            STATE_ATTR_CURRENT_MONTH_PRICE_WITH_IMPACT: self._current_month_energy_price_with_impact,
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }

    def update(self):
        _login_helen_api_if_needed(self._api_client, self.credentials)
        _select_delivery_site(self._api_client, self._delivery_site_id)
        self._contract_base_price = self._api_client.get_contract_base_price()
        current_month_total_consumption = self._current_month_consumption = math.ceil(
            _get_total_consumption_for_current_month(self._api_client)
        )
        self._current_month_consumption = current_month_total_consumption
        self._last_month_consumption = math.ceil(
            _get_total_consumption_for_last_month(self._api_client)
        )
        current_month = date.today()
        current_month_impact = self._api_client.calculate_impact_of_usage_between_dates(
            *get_month_date_range_by_date(current_month)
        )

        try:
            fetched_base_price = self._api_client.get_contract_base_price()
            self._contract_base_price = fetched_base_price
            self._latest_base_price = fetched_base_price  # save the latest value
        except InvalidApiResponseException:
            _LOGGER.error(
                "Received invalid response from Helen API when fetching contract base price - using the latest value if it exists, or 0 if it doesn't"
            )
            self._contract_base_price = (
                self._latest_base_price if self._latest_base_price is not None else 0
            )

        if self._default_base_price is not None:
            _LOGGER.info(f"Using the default base price: {self._default_base_price}")
            self._contract_base_price = self._default_base_price

        unit_price = 0

        try:
            unit_price = self._api_client.get_contract_energy_unit_price()
            self._latest_unit_price = unit_price  # save the latest value
        except InvalidApiResponseException:
            _LOGGER.error(
                "Received invalid response from Helen API when fetching energy unti price - using the latest value if it exists, or 0 if it doesn't"
            )
            unit_price = (
                self._latest_unit_price if self._latest_unit_price is not None else 0
            )

        if self._default_unit_price is not None:
            _LOGGER.info(
                f"Using the default energy unit price: {self._default_unit_price}"
            )
            unit_price = self._default_unit_price

        current_month_energy_price_with_impact = (
            unit_price + current_month_impact
        ) / 100
        self._current_month_energy_price_with_impact = (
            current_month_energy_price_with_impact
        )
        current_month_total_cost = (
            current_month_total_consumption * current_month_energy_price_with_impact
            + self._contract_base_price
        )
        self._state = math.ceil(current_month_total_cost)
        self._current_month_consumption = current_month_total_consumption
        self._average_daily_consumption = math.ceil(
            _get_average_daily_consumption_for_current_month(self._api_client)
        )
        self._current_month_consumption = current_month_total_consumption
        self._api_client.close()


class HelenFixedPriceElectricity(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}
    _contract_base_price = None
    _last_month_consumption = None
    _current_month_consumption = None
    _fixed_unit_price = None
    _average_daily_consumption = None
    _latest_base_price = None
    _latest_unit_price = None
    _delivery_site_id = None

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials,
        default_base_price,
        default_unit_price,
        delivery_site_id,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "helen_fixed_price_electricity"
        self._name = "Helen Fixed Price Electricity"
        self._api_client = helen_api_client
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE
        self._default_base_price = default_base_price
        self._default_unit_price = default_unit_price
        self._delivery_site_id = delivery_site_id

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self.id

    @property
    def state_attributes(self) -> Dict[str, Any]:
        return self.attrs

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the extra state attributes of the measurement."""
        return {
            STATE_ATTR_CONTRACT_BASE_PRICE: self._contract_base_price,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: self._last_month_consumption,
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: self._current_month_consumption,
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: self._average_daily_consumption,
            STATE_ATTR_FIXED_UNIT_PRICE: self._fixed_unit_price,
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
            STATE_ATTR_FIXED_UNIT_PRICE_UNIT_OF_MEASUREMENT: "c/kWh",
        }

    def update(self):
        _login_helen_api_if_needed(self._api_client, self.credentials)
        _select_delivery_site(self._api_client, self._delivery_site_id)
        self._contract_base_price = self._api_client.get_contract_base_price()
        current_month_total_consumption = self._current_month_consumption = math.ceil(
            _get_total_consumption_for_current_month(self._api_client)
        )
        self._current_month_consumption = current_month_total_consumption
        self._last_month_consumption = math.ceil(
            _get_total_consumption_for_last_month(self._api_client)
        )

        try:
            fetched_base_price = self._api_client.get_contract_base_price()
            self._contract_base_price = fetched_base_price
            self._latest_base_price = fetched_base_price  # save the latest value
        except InvalidApiResponseException:
            _LOGGER.error(
                "Received invalid response from Helen API when fetching contract base price - using the latest value if it exists, or 0 if it doesn't"
            )
            self._contract_base_price = (
                self._latest_base_price if self._latest_base_price is not None else 0
            )

        if self._default_base_price is not None:
            _LOGGER.info(f"Using the default base price: {self._default_base_price}")
            self._contract_base_price = self._default_base_price

        unit_price = 0

        try:
            unit_price = self._api_client.get_contract_energy_unit_price()
            self._latest_unit_price = unit_price  # save the latest value
        except InvalidApiResponseException:
            _LOGGER.error(
                "Received invalid response from Helen API when fetching energy unit price - using the latest value if it exists, or 0 if it doesn't"
            )
            unit_price = (
                self._latest_unit_price if self._latest_unit_price is not None else 0
            )

        if self._default_unit_price is not None:
            _LOGGER.info(
                f"Using the default energy unit price: {self._default_unit_price}"
            )
            unit_price = self._default_unit_price

        self._fixed_unit_price = unit_price
        current_month_total_cost = (
            current_month_total_consumption * unit_price / 100
            + self._contract_base_price
        )
        self._state = math.ceil(current_month_total_cost)
        self._current_month_consumption = current_month_total_consumption
        self._average_daily_consumption = math.ceil(
            _get_average_daily_consumption_for_current_month(self._api_client)
        )
        self._current_month_consumption = current_month_total_consumption
        self._api_client.close()


class HelenTransferPrice(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "EUR", "icon": "mdi:currency-eur"}

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        credentials,
        delivery_site_id,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "helen_transfer_costs"
        self._name = "Helen Transfer Costs"
        self._api_client = helen_api_client
        self._state = STATE_UNAVAILABLE
        self._delivery_site_id = delivery_site_id

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self.id

    @property
    def state_attributes(self) -> Dict[str, Any]:
        return self.attrs

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def state(self) -> Optional[str]:
        return self._state

    def update(self):
        _login_helen_api_if_needed(self._api_client, self.credentials)
        _select_delivery_site(self._api_client, self._delivery_site_id)
        self._state = get_transfer_price_total_for_current_month(self._api_client)
        self._api_client.close()


class HelenMonthlyConsumption(SensorEntity):
    _attr_name = "Helen Monthly Consumption"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:home-lightning-bolt"

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        credentials,
        delivery_site_id,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "helen_monthly_consumption"
        self._api_client = helen_api_client
        self._delivery_site_id = delivery_site_id

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the sensor."""
        return self.id

    def update(self) -> None:
        _login_helen_api_if_needed(self._api_client, self.credentials)
        _select_delivery_site(self._api_client, self._delivery_site_id)
        self._attr_native_value = _get_total_consumption_for_current_month(
            self._api_client
        )
        self._api_client.close()
