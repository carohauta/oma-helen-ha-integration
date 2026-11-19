"""Data update coordinator for the Helen Energy integration."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import requests
from dateutil.relativedelta import relativedelta
from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import InvalidApiResponseException
from helenservice.price_client import HelenPriceClient
from helenservice.utils import get_month_date_range_by_date
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_CONTRACT_START_DATE,
    CONF_CONTRACT_TYPE,
    CONF_DEFAULT_UNIT_PRICE,
    CONTRACT_TYPE_EXCHANGE,
    CONTRACT_TYPE_MARKET,
    CONTRACT_TYPE_SMART,
    OMA_HELEN_API_V28_BASE,
)
from .statistics import HelenStatisticsManager
from .utils import conf, get_entry_position, resolve_contract_type, safe_round

if TYPE_CHECKING:
    from helenservice.api_response import MeasurementsWithSpotPriceResponse
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=3)


def _classify_consumption_impact(period_value: dict[str, Any]) -> str:
    """Classify consumption recommendation into Lowering / Neutral / Raising."""
    raw_text = " ".join(
        str(v)
        for v in (
            period_value.get("level"),
            period_value.get("status"),
            period_value.get("type"),
            period_value.get("header", {}).get("fi"),
            period_value.get("info", {}).get("fi"),
            period_value.get("extra_info", {}).get("fi"),
        )
        if v is not None
    ).lower()

    if any(
        word in raw_text
        for word in ("vähennä", "kalliim", "nostava", "high", "red", "expensive")
    ):
        return "Raising"
    if any(
        word in raw_text
        for word in (
            "keskitä",
            "halvem",
            "alentava",
            "low",
            "green",
            "cheap",
            "normaalisti",
        )
    ):
        return "Lowering"
    return "Neutral"


def _extract_active_rented_solar_contract(
    contracts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find an active rented-production (Nimikkopaneeli) contract from contract JSON."""
    now_iso = datetime.now(ZoneInfo("UTC")).isoformat()
    for contract in contracts:
        end_date = contract.get("end_date") or contract.get("contract_end_date")
        if end_date and str(end_date) < now_iso[:19]:
            continue
        for product in contract.get("products", []):
            if product.get("product_type") == "rented-production":
                attrs = product.get("attributes") or {}
                return {
                    "plant_id": product.get("id"),
                    "name": product.get("name"),
                    "panel_amount": int(attrs.get("panel_amount", 1)),
                    "location": attrs.get("location"),
                }
    return None


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
        self.contract_type = conf(config_entry, CONF_CONTRACT_TYPE)

        is_first_entry, entry_index = get_entry_position(hass, config_entry)
        if is_first_entry:
            entity_id = "sensor.helen_monthly_consumption"
        else:
            entity_id = f"sensor.helen_monthly_consumption_{entry_index + 1}"

        config_fixed_unit_price = conf(config_entry, CONF_DEFAULT_UNIT_PRICE)

        self.statistics_manager = HelenStatisticsManager(
            hass,
            helen_api_client,
            entity_id,
            config_entry.entry_id,
            config_entry.title,
            fixed_unit_price=config_fixed_unit_price,
        )

    async def _async_update_data(self):
        """Fetch data from Helen API."""
        try:
            await _login_helen_api_if_needed(
                self.hass, self.api_client, self.credentials
            )
            _select_delivery_site(self.api_client, self.delivery_site_id)

            _LOGGER.debug("Starting data fetch from Helen API")
            data: dict[str, Any] = {}

            data[
                "current_month_consumption"
            ] = await _get_total_consumption_for_current_month(
                self.hass, self.api_client
            )
            data[
                "last_month_consumption"
            ] = await _get_total_consumption_for_last_month(self.hass, self.api_client)
            data[
                "daily_average_consumption"
            ] = await _get_average_daily_consumption_for_current_month(
                self.hass, self.api_client
            )

            if self.include_transfer_costs:
                data[
                    "transfer_costs"
                ] = await _get_transfer_price_total_for_current_month(
                    self.hass, self.api_client
                )
            else:
                data["transfer_costs"] = 0.0

            data["contract_base_price"] = await self.hass.async_add_executor_job(
                self.api_client.get_contract_base_price
            )
            data["contract_type"] = await self.hass.async_add_executor_job(
                self.api_client.get_contract_type
            )

            current_contract_start = await self.hass.async_add_executor_job(
                self.api_client.get_contract_start_date
            )

            if current_contract_start is not None:
                stored_contract_start = self.config_entry.data.get(
                    CONF_CONTRACT_START_DATE
                )

                if stored_contract_start is None:
                    _LOGGER.info(
                        "Initial contract start date: %s", current_contract_start
                    )
                    new_data = dict(self.config_entry.data)
                    new_data[CONF_CONTRACT_START_DATE] = (
                        current_contract_start.isoformat()
                    )
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=new_data
                    )
                else:
                    stored_date = date.fromisoformat(stored_contract_start)
                    if (
                        current_contract_start != stored_date
                        and current_contract_start > stored_date
                    ):
                        _LOGGER.warning(
                            "Contract renewal detected! Old start: %s, New start: %s",
                            stored_date,
                            current_contract_start,
                        )
                        new_data = dict(self.config_entry.data)
                        new_data[CONF_CONTRACT_START_DATE] = (
                            current_contract_start.isoformat()
                        )
                        self.hass.config_entries.async_update_entry(
                            self.config_entry, data=new_data
                        )

                data["contract_start_date"] = current_contract_start

            try:
                unit_price = await self.hass.async_add_executor_job(
                    self.api_client.get_contract_energy_unit_price
                )
            except InvalidApiResponseException as e:
                _LOGGER.debug("Failed to get unit price: %s", e)
                unit_price = None

            if not unit_price and data.get("contract_type") is None:
                try:
                    unit_price = await self.hass.async_add_executor_job(
                        self.api_client.get_transfer_fee
                    )
                except InvalidApiResponseException as e:
                    _LOGGER.debug("Failed to get transfer fee: %s", e)
                    unit_price = None
            data["unit_price"] = unit_price

            effective_contract_type = resolve_contract_type(
                self.contract_type, data.get("contract_type")
            )

            # Market prices
            if effective_contract_type == CONTRACT_TYPE_MARKET:
                try:
                    prices = await self.hass.async_add_executor_job(
                        self.price_client.get_market_price_prices
                    )
                    if prices is not None:
                        data["market_prices"] = {
                            "last_month": getattr(prices, "last_month", None),
                            "current_month": getattr(prices, "current_month", None),
                            "next_month": getattr(prices, "next_month", None),
                        }
                    else:
                        data["market_prices"] = None
                except (InvalidApiResponseException, AttributeError) as e:
                    _LOGGER.debug("Failed to get market prices: %s", e)
                    data["market_prices"] = None
            else:
                data["market_prices"] = None

            # Exchange costs
            if effective_contract_type == CONTRACT_TYPE_EXCHANGE:
                try:
                    current_month = date.today()
                    last_month = current_month + relativedelta(months=-1)

                    current_month_cost = await self.hass.async_add_executor_job(
                        self.api_client.calculate_total_costs_by_spot_prices_between_dates,
                        *get_month_date_range_by_date(current_month),
                    )

                    try:
                        last_month_cost = await self.hass.async_add_executor_job(
                            self.api_client.calculate_total_costs_by_spot_prices_between_dates,
                            *get_month_date_range_by_date(last_month),
                        )
                    except InvalidApiResponseException as err:
                        if "no-relevant-contract" in str(err).lower():
                            last_month_cost = 0.0
                        else:
                            raise

                    data["exchange_costs"] = {
                        "current_month": safe_round(current_month_cost),
                        "last_month": safe_round(last_month_cost),
                    }
                except InvalidApiResponseException:
                    data["exchange_costs"] = None
            else:
                data["exchange_costs"] = None

            # Smart electricity (Välkky / Valtti) usage impact & recommendations
            if effective_contract_type == CONTRACT_TYPE_SMART:
                data["smart_electricity"] = await _fetch_smart_electricity_data(
                    self.hass, self.api_client
                )
            else:
                data["smart_electricity"] = None

            # Rented solar panel (Nimikkopaneeli) production if present on account
            data["rented_solar"] = await _fetch_rented_solar_data(
                self.hass, self.api_client
            )

            if self.data is not None:
                for key in (
                    "current_month_consumption",
                    "last_month_consumption",
                    "daily_average_consumption",
                ):
                    if data.get(key) is None and self.data.get(key) is not None:
                        data[key] = self.data[key]

        except InvalidApiResponseException as err:
            if "authentication" in str(err).lower():
                raise ConfigEntryAuthFailed from err
            _LOGGER.warning(
                "Error communicating with Helen API, keeping last known values: %s", err
            )
            return self.data if self.data is not None else {}
        except Exception as err:
            _LOGGER.error(
                "Unexpected error fetching Helen data, keeping last known values: %s",
                err,
                exc_info=True,
            )
            return self.data if self.data is not None else {}
        else:
            if self.statistics_manager:
                if conf(self.config_entry, CONF_DEFAULT_UNIT_PRICE) is None:
                    self.statistics_manager.set_fixed_unit_price(data.get("unit_price"))

                try:
                    await self.statistics_manager.import_recent_statistics()
                except InvalidApiResponseException as err:
                    _LOGGER.warning("Statistics import API error: %s", err)
                except Exception as err:
                    _LOGGER.error(
                        "Unexpected error importing statistics: %s", err, exc_info=True
                    )

            return data
        finally:
            self.api_client.close()


async def _fetch_smart_electricity_data(
    hass: HomeAssistant, helen_api_client: HelenApiClient
) -> dict[str, Any]:
    """Fetch usage impact and consumption status recommendations for smart contracts."""
    today = date.today()
    last_month = today + relativedelta(months=-1)
    curr_start, curr_end = get_month_date_range_by_date(today)
    curr_end = min(curr_end, today)
    last_start, last_end = get_month_date_range_by_date(last_month)

    def _calc_impact(start_d: date, end_d: date) -> float | None:
        for method_name in (
            "calculate_impact_of_usage_between_dates",
            "calculate_the_impact_of_usage_between_dates",
        ):
            fn = getattr(helen_api_client, method_name, None)
            if callable(fn):
                return fn(start_d, end_d)
        return None

    current_impact = None
    last_impact = None
    try:
        current_impact = await hass.async_add_executor_job(
            _calc_impact, curr_start, curr_end
        )
    except Exception as err:
        _LOGGER.warning("Failed to calculate current month usage impact: %s", err)

    try:
        last_impact = await hass.async_add_executor_job(
            _calc_impact, last_start, last_end
        )
    except Exception as err:
        _LOGGER.warning("Failed to calculate last month usage impact: %s", err)

    def _get_status_periods() -> list[dict[str, Any]]:
        token = helen_api_client.get_api_access_token()
        resp = requests.get(
            f"{OMA_HELEN_API_V28_BASE}/electricity-status-indicator/today",
            params={"hours": 24},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json() or []

    periods: list[dict[str, Any]] = []
    try:
        periods = await hass.async_add_executor_job(_get_status_periods)
    except Exception as err:
        _LOGGER.debug("Failed to fetch electricity status indicator: %s", err)

    current_status = None
    current_header = None
    current_info = None
    forecast: list[dict[str, Any]] = []

    if periods:
        current_val = periods[0].get("value", {})
        current_status = _classify_consumption_impact(current_val)
        current_header = current_val.get("header", {}).get("fi")
        current_info = current_val.get("info", {}).get("fi")

        for p in periods:
            pval = p.get("value", {})
            spot_no_vat = pval.get("value")
            forecast.append(
                {
                    "start": p.get("start"),
                    "stop": p.get("stop"),
                    "impact": _classify_consumption_impact(pval),
                    "header": pval.get("header", {}).get("fi"),
                    "spot_price_vat": safe_round(spot_no_vat * 1.255, 2)
                    if spot_no_vat is not None
                    else None,
                }
            )

    return {
        "current_month_impact": safe_round(current_impact, 2)
        if current_impact is not None
        else None,
        "last_month_impact": safe_round(last_impact, 2)
        if last_impact is not None
        else None,
        "recommendation": current_status,
        "header": current_header,
        "info": current_info,
        "forecast": forecast,
    }


async def _fetch_rented_solar_data(
    hass: HomeAssistant, helen_api_client: HelenApiClient
) -> dict[str, Any] | None:
    """Detect active rented solar panels and fetch monthly/daily production."""

    def _fetch_sync() -> dict[str, Any] | None:
        contracts = helen_api_client.get_contract_data_json() or []
        solar_contract = _extract_active_rented_solar_contract(contracts)
        if not solar_contract or not solar_contract.get("plant_id"):
            return None

        plant_id = solar_contract["plant_id"]
        panel_amount = solar_contract["panel_amount"]
        token = helen_api_client.get_api_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        fi_tz = ZoneInfo("Europe/Helsinki")
        now = datetime.now(fi_tz)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        month_start = today_start.replace(day=1)

        resp = requests.get(
            f"{OMA_HELEN_API_V28_BASE}/measurements/{plant_id}/one-share",
            params={
                "begin": month_start.astimezone(ZoneInfo("UTC")).isoformat(),
                "end": tomorrow_start.astimezone(ZoneInfo("UTC")).isoformat(),
                "resolution": "day",
            },
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        intervals = (
            resp.json().get("intervals", {}).get("rented_production_one_panel", [])
        )

        daily_kwh: list[float] = []
        if intervals:
            for m in intervals[0].get("measurements", []):
                if m.get("value") is not None and m.get("status") == "valid":
                    daily_kwh.append(round(float(m["value"]) * panel_amount, 3))

        month_kwh = round(sum(daily_kwh), 3) if daily_kwh else 0.0
        latest_day_kwh = (
            daily_kwh[-2]
            if len(daily_kwh) >= 2
            else (daily_kwh[-1] if daily_kwh else 0.0)
        )

        return {
            "plant_id": plant_id,
            "name": solar_contract.get("name"),
            "location": solar_contract.get("location"),
            "panel_amount": panel_amount,
            "month_kwh": month_kwh,
            "latest_day_kwh": latest_day_kwh,
            "daily_kwh": daily_kwh,
        }

    try:
        return await hass.async_add_executor_job(_fetch_sync)
    except Exception as err:
        _LOGGER.debug("Failed to fetch rented solar panel data: %s", err)
        return None


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
    measurement_response: MeasurementsWithSpotPriceResponse = (
        await hass.async_add_executor_job(
            helen_api_client.get_daily_measurements_between_dates, start_date, end_date
        )
    )
    if not measurement_response.series:
        return None
    total = sum(
        entry.electricity
        for entry in measurement_response.series
        if entry.electricity is not None
    )
    return safe_round(total)


async def _get_total_consumption_for_last_month(
    hass: HomeAssistant, helen_api_client: HelenApiClient
) -> float:
    """Get total consumption for last month."""
    today_last_month = date.today() + relativedelta(months=-1)
    start_date, end_date = get_month_date_range_by_date(today_last_month)

    try:
        return await _get_total_consumption_between_dates(
            hass, helen_api_client, start_date, end_date
        )
    except InvalidApiResponseException as err:
        if "no-relevant-contract" in str(err).lower():
            return 0.0
        raise


async def _get_total_consumption_for_current_month(
    hass: HomeAssistant, helen_api_client: HelenApiClient
):
    """Get total consumption for current month."""
    start_date, end_date = get_month_date_range_by_date(date.today())
    return await _get_total_consumption_between_dates(
        hass, helen_api_client, start_date, end_date
    )


async def _get_transfer_price_total_for_current_month(
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
    measurement_response: MeasurementsWithSpotPriceResponse = (
        await hass.async_add_executor_job(
            helen_api_client.get_daily_measurements_between_dates, start_date, end_date
        )
    )
    if not measurement_response.series:
        return None
    valid_measurements = [
        entry.electricity
        for entry in measurement_response.series
        if entry.electricity is not None
    ]
    if not valid_measurements:
        return None
    return safe_round(sum(valid_measurements) / len(valid_measurements))