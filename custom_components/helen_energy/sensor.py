from datetime import datetime, timedelta
import logging
import math
from typing import Any, Dict, Optional

from .api_response import MeasurementResponse
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
from .const import (
    DOMAIN,
)
from .price_client import HelenPriceClient, HelenContractType
from .api_client import HelenApiClient

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=6)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_USERNAME): cv.string, vol.Required(CONF_PASSWORD): cv.string}
)

STATE_ATTR_DAILY_AVERAGE_CONSUMPTION = "daily_average_consumption"
STATE_ATTR_CURRENT_MONTH_CONSUMPTION = "current_month_consumption"
STATE_ATTR_LAST_MONTH_CONSUMPTION = "last_month_consumption"
STATE_ATTR_CONSUMPTION_UNIT_OF_MEASUREMENT = "consumption_unit_of_measurement"
STATE_ATTR_CONTRACT_BASE_PRICE = "contract_base_price"
STATE_ATTR_LAST_MONTH_COST = "last_month_cost"


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType = None,
) -> None:
    """Set up the Helen Energy platform."""
    username = config[CONF_USERNAME]
    password = config.get(CONF_PASSWORD)

    helen_api_client = HelenApiClient()
    helen_price_client = HelenPriceClient(HelenContractType.MARKET_PRICE)
    credentials = {"username": username, "password": password}

    # TODO: utilize DataUpdateCoordinator class and prefetch all data here, in setup

    add_entities(
        [
            HelenPrice(helen_price_client, "last_month"),
            HelenPrice(helen_price_client, "current_month"),
            HelenPrice(helen_price_client, "next_month"),
            HelenCostEstimate(helen_api_client, helen_price_client, credentials),
        ],
        True, # TODO: remove after utilizing DataUpdateCoordinator
    )


def _get_total_consumption_by_month(helen_api_client, index, year):
    measurement_response = helen_api_client.get_monthly_measurements_by_year(year)
    measurement_value = (
        measurement_response.intervals.electricity[0].measurements[index].value
    )
    return measurement_value


def _get_total_consumption_for_last_month(helen_api_client):
    """Total consumption for last month"""
    now = datetime.now()
    return _get_total_consumption_by_month(helen_api_client, now.month - 2, now.year)


def _get_total_consumption_for_current_month(helen_api_client):
    """Total consumption for current month"""
    now = datetime.now()
    return _get_total_consumption_by_month(helen_api_client, now.month - 1, now.year)


def _get_average_daily_consumption_for_current_month(helen_api_client):
    """Average daily consumption for current month"""
    now = datetime.now()
    measurement_response: MeasurementResponse = (
        helen_api_client.get_daily_measurements_by_month(now.month)
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
    daily_average = sum(valid_measurements) / len(valid_measurements)
    return daily_average


class HelenCostEstimate(Entity):
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
        if not self._api_client.is_session_valid():
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


class HelenPrice(Entity):
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
        prices = self._price_client.get_electricity_prices()
        self._state = getattr(prices, self.key)
