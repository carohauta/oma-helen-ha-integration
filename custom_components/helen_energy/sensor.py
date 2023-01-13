from datetime import date, datetime, timedelta
import logging
import math
from typing import Any, Dict, Optional

from dateutil.relativedelta import relativedelta
from helenservice.api_response import MeasurementResponse
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import PLATFORM_SCHEMA, SCAN_INTERVAL
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_UNAVAILABLE
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
)
import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA
from .const import DOMAIN, CONF_VAT, CONF_CONTRACT_TYPE
from helenservice.price_client import HelenPriceClient
from helenservice.api_client import HelenApiClient
from helenservice.utils import get_month_date_range_by_date

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=2)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_VAT): cv.positive_float,
        vol.Required(CONF_CONTRACT_TYPE): cv.string,
    }
)

STATE_ATTR_DAILY_AVERAGE_CONSUMPTION = "daily_average_consumption"
STATE_ATTR_CURRENT_MONTH_CONSUMPTION = "current_month_consumption"
STATE_ATTR_LAST_MONTH_CONSUMPTION = "last_month_consumption"
STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT = "consumption_unit_of_measurement"
STATE_ATTR_CONTRACT_BASE_PRICE = "contract_base_price"
STATE_ATTR_LAST_MONTH_COST = "last_month_cost"
STATE_ATTR_LAST_MONTH_TOTAL_COST = "last_month_total_cost"
STATE_ATTR_CURRENT_MONTH_TOTAL_COST = "current_month_total_cost"
STATE_ATTR_LAST_MONTH_PRICE_WITH_IMPACT = "last_month_price_with_impact"
STATE_ATTR_CURRENT_MONTH_PRICE_WITH_IMPACT = "current_month_price_with_impact"


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

    helen_price_client = HelenPriceClient()

    # initial margin
    margin = helen_price_client.get_exchange_prices().margin
    helen_api_client = HelenApiClient(vat, margin)

    credentials = {"username": username, "password": password}

    entities = []

    if contract_type == "MARKET":
        entities.append(HelenMarketPrice(helen_price_client, "last_month"))
        entities.append(HelenMarketPrice(helen_price_client, "current_month"))
        entities.append(HelenMarketPrice(helen_price_client, "next_month"))
        entities.append(
            HelenMarketPriceCostEstimate(
                helen_api_client, helen_price_client, credentials
            )
        )
    elif contract_type == "EXCHANGE":
        entities.append(
            HelenExchangeCosts(helen_api_client, helen_price_client, credentials)
        )
    elif contract_type == "SMART_GUARANTEE":
        entities.append(
            HelenSmartGuaranteeCosts(helen_api_client, helen_price_client, credentials)
        )

    add_entities(
        entities,
        True,
    )


def _get_total_consumption_between_dates(
    helen_api_client: HelenApiClient, start_date: date, end_date: date
):
    measurement_response: MeasurementResponse = (
        helen_api_client.get_daily_measurements_between_dates(start_date, end_date)
    )
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


def _get_average_daily_consumption_for_current_month(helen_api_client: HelenApiClient):
    """Average daily consumption for current month"""
    start_date, end_date = get_month_date_range_by_date(date.today())
    measurement_response: MeasurementResponse = (
        helen_api_client.get_daily_measurements_between_dates(start_date, end_date)
    )
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


class HelenMarketPriceCostEstimate(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "e", "icon": "mdi:currency-eur"}
    _contract_base_price = None
    _last_month_cost = None
    _current_month_consumption = None
    _average_daily_consumption = None
    _last_month_measurement = None

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "measurement_cost_estimate"
        self._name = "Helen energy cost estimate"
        self._api_client = helen_api_client
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE

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
            STATE_ATTR_LAST_MONTH_COST: self._last_month_cost,
            STATE_ATTR_CURRENT_MONTH_CONSUMPTION: self._current_month_consumption,
            STATE_ATTR_LAST_MONTH_CONSUMPTION: self._last_month_measurement,
            STATE_ATTR_DAILY_AVERAGE_CONSUMPTION: self._average_daily_consumption,
            STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT: "kWh",
        }

    def _calculate_last_month_price(self):
        prices = self._price_client.get_electricity_prices()
        last_month_price = getattr(prices, "last_month") / 100
        last_month_consumption = _get_total_consumption_for_last_month(self._api_client)
        last_month_cost = (
            last_month_price * last_month_consumption + self._contract_base_price
        )
        return last_month_cost

    def _calculate_current_month_price_estimate(self):
        prices = self._price_client.get_electricity_prices()
        current_month_price = getattr(prices, "current_month") / 100
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
        self._api_client.login(**self.credentials)

        self._contract_base_price = self._api_client.get_contract_base_price()
        self._state = self._calculate_current_month_price_estimate()
        self._last_month_cost = self._calculate_last_month_price()
        self._average_daily_consumption = (
            _get_average_daily_consumption_for_current_month(self._api_client)
        )
        self._current_month_consumption = _get_total_consumption_for_current_month(
            self._api_client
        )
        self._last_month_measurement = _get_total_consumption_for_last_month(
            self._api_client
        )
        self._api_client.close()


class HelenMarketPrice(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "c/kWh", "icon": "mdi:currency-eur"}

    def __init__(self, helen_price_client: HelenPriceClient, key: str):
        super().__init__()
        self.key = key
        self.id = "price_" + key
        self._name = "Helen energy price " + key.replace("_", " ")
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE

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
        prices = self._price_client.get_market_price_prices()
        self._state = getattr(prices, self.key)


class HelenExchangeCosts(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "e", "icon": "mdi:currency-eur"}
    _contract_base_price = None
    _last_month_total_cost = None
    _last_month_consumption = None
    _current_month_consumption = None
    _average_daily_consumption = None

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "exchange_electricity_calculations"
        self._name = "Helen Exchange energy cost calculations"
        self._api_client = helen_api_client
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE

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
        self._api_client.login(**self.credentials)
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
        self._contract_base_price = self._api_client.get_contract_base_price()

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


class HelenSmartGuaranteeCosts(Entity):
    attrs: Dict[str, Any] = {"unit_of_measurement": "e", "icon": "mdi:currency-eur"}
    _contract_base_price = None
    _last_month_consumption = None
    _current_month_consumption = None
    _current_month_energy_price_with_impact = None
    _average_daily_consumption = None

    def __init__(
        self,
        helen_api_client: HelenApiClient,
        helen_price_client: HelenPriceClient,
        credentials,
    ):
        super().__init__()
        self.credentials = credentials
        self.id = "smart_guarantee_electricity_calculations"
        self._name = "Helen Smart Guarantee energy cost calculations"
        self._api_client = helen_api_client
        self._price_client = helen_price_client
        self._state = STATE_UNAVAILABLE

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
        self._api_client.login(**self.credentials)
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
        current_month_energy_price_with_impact = (
            self._price_client.get_smart_guarantee_prices().price + current_month_impact
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
