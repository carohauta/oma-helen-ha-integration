"""Data update coordinator for the Helen Energy integration."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

from dateutil.relativedelta import relativedelta
from helenservice.api_client import HelenApiClient
from helenservice.api_exceptions import InvalidApiResponseException
from helenservice.price_client import HelenPriceClient
from helenservice.utils import get_month_date_range_by_date
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_DEFAULT_UNIT_PRICE, DOMAIN
from .statistics import HelenStatisticsManager
from .utils import safe_round

if TYPE_CHECKING:
    from helenservice.api_response import MeasurementsWithSpotPriceResponse
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=3)


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

        # Initialize statistics manager
        # Generate entity_id for monthly consumption sensor
        # For the first entry, use the standard entity ID
        helen_entries = list(hass.config_entries.async_entries(DOMAIN))
        is_first_entry = (
            len(helen_entries) >= 1 and helen_entries[0] == config_entry
        )

        if is_first_entry:
            entity_id = "sensor.helen_monthly_consumption"
        else:
            # For additional entries, construct entity_id with suffix
            entry_index = next(
                (
                    i
                    for i, entry in enumerate(helen_entries)
                    if entry == config_entry
                ),
                1,
            )
            entity_id = f"sensor.helen_monthly_consumption_{entry_index + 1}"

        # Get user-configured fixed unit price (if any)
        config_fixed_unit_price = config_entry.data.get(CONF_DEFAULT_UNIT_PRICE)

        self.statistics_manager = HelenStatisticsManager(
            hass,
            helen_api_client,
            entity_id,
            config_entry.entry_id,
            config_entry.title,
            fixed_unit_price=config_fixed_unit_price,
        )
        _LOGGER.debug(
            "Statistics manager initialized for %s (fixed_price=%s)",
            entity_id,
            f"{config_fixed_unit_price} cents/kWh" if config_fixed_unit_price else "from API",
        )

    async def _async_update_data(self):
        """Fetch data from Helen API."""
        try:
            await _login_helen_api_if_needed(
                self.hass, self.api_client, self.credentials
            )
            _select_delivery_site(self.api_client, self.delivery_site_id)

            # Get all the data we need
            _LOGGER.debug("Starting data fetch from Helen API")

            data = {}

            _LOGGER.debug("Fetching current month consumption")
            data[
                "current_month_consumption"
            ] = await _get_total_consumption_for_current_month(
                self.hass, self.api_client
            )

            _LOGGER.debug("Fetching last month consumption")
            data[
                "last_month_consumption"
            ] = await _get_total_consumption_for_last_month(self.hass, self.api_client)

            _LOGGER.debug("Fetching daily average consumption")
            data[
                "daily_average_consumption"
            ] = await _get_average_daily_consumption_for_current_month(
                self.hass, self.api_client
            )

            if self.include_transfer_costs:
                _LOGGER.debug("Fetching transfer costs")
                data[
                    "transfer_costs"
                ] = await _get_transfer_price_total_for_current_month(
                    self.hass, self.api_client
                )
            else:
                data["transfer_costs"] = 0.0

            _LOGGER.debug("Fetching contract base price")
            data["contract_base_price"] = await self.hass.async_add_executor_job(
                self.api_client.get_contract_base_price
            )

            _LOGGER.debug("Fetching contract type")
            data["contract_type"] = await self.hass.async_add_executor_job(
                self.api_client.get_contract_type
            )

            # Get prices based on contract type
            try:
                _LOGGER.debug("Fetching unit price")
                data["unit_price"] = await self.hass.async_add_executor_job(
                    self.api_client.get_contract_energy_unit_price
                )
            except InvalidApiResponseException as e:
                _LOGGER.debug("Failed to get unit price: %s", e)
                data["unit_price"] = None

            # Get market prices if needed
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

            # Get exchange prices if needed
            try:
                exchange_prices = await self.hass.async_add_executor_job(
                    self.price_client.get_exchange_prices
                )
                if exchange_prices is not None:
                    data["exchange_prices"] = {"margin": exchange_prices.margin}
                else:
                    data["exchange_prices"] = None
            except (InvalidApiResponseException, AttributeError) as e:
                _LOGGER.debug("Failed to get exchange prices: %s", e)
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
                    "current_month_impact": safe_round(current_month_impact),
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
            _LOGGER.error(
                "Unexpected error fetching Helen data, keeping last known values: %s",
                err,
            )
            _LOGGER.error("Exception traceback:", exc_info=True)
            # Return the existing data if available, otherwise return empty dict
            return self.data if self.data is not None else {}
        else:
            # Import statistics after successful data fetch
            if self.statistics_manager:
                # Update fixed unit price from API if not configured by user
                # Priority: 1. User config, 2. API contract price
                config_price = self.config_entry.data.get(CONF_DEFAULT_UNIT_PRICE)
                if config_price is None:
                    # Use API price if available (only for fixed-price contracts)
                    self.statistics_manager._fixed_unit_price = data.get("unit_price")

                try:
                    await self.statistics_manager.import_recent_statistics()
                    _LOGGER.debug("Successfully imported energy statistics")
                except InvalidApiResponseException as err:
                    # Follow existing error pattern - log warning but don't fail
                    _LOGGER.warning("Statistics import API error: %s", err)
                except Exception as err:
                    _LOGGER.error(
                        "Unexpected error importing statistics: %s", err, exc_info=True
                    )
                    # Don't fail coordinator update

            return data
        finally:
            self.api_client.close()


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
        return 0.0
    total = sum(
        entry.electricity
        for entry in measurement_response.series
        if entry.electricity is not None
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
        return 0
    valid_measurements = [
        entry.electricity
        for entry in measurement_response.series
        if entry.electricity is not None
    ]
    average = (
        sum(valid_measurements) / len(valid_measurements) if valid_measurements else 0
    )
    return safe_round(average)
