"""Tests for the Helen Energy backfill service."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.helen_energy.const import (
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    SERVICE_BACKFILL_STATISTICS,
)
from custom_components.helen_energy.services import async_setup_services


@pytest.fixture
def mock_coordinator(hass: HomeAssistant):
    """Register a single coordinator whose backfill calls are recorded."""
    coordinator = Mock()
    coordinator.hass = hass
    coordinator.api_client.is_session_valid.return_value = True
    coordinator.statistics_manager.entity_id = "sensor.helen_monthly_consumption"
    coordinator.statistics_manager.backfill_statistics = AsyncMock()

    hass.data[DOMAIN] = {"entry_1": {"coordinator": coordinator}}
    return coordinator


class TestBackfillService:
    """The service's date-range handling."""

    async def test_omitted_start_date_defaults_to_thirty_days(
        self, hass: HomeAssistant, mock_coordinator
    ):
        """Calling the action with no start_date backfills DEFAULT_BACKFILL_DAYS."""
        await async_setup_services(hass)

        await hass.services.async_call(
            DOMAIN, SERVICE_BACKFILL_STATISTICS, {}, blocking=True
        )

        start_date, end_date = (
            mock_coordinator.statistics_manager.backfill_statistics.call_args[0]
        )
        assert end_date == date.today()
        assert start_date == date.today() - timedelta(days=DEFAULT_BACKFILL_DAYS)

    async def test_explicit_start_date_is_passed_through(
        self, hass: HomeAssistant, mock_coordinator
    ):
        """An explicit start_date wins over the default."""
        await async_setup_services(hass)

        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKFILL_STATISTICS,
            {"start_date": "2024-01-01"},
            blocking=True,
        )

        start_date, _ = (
            mock_coordinator.statistics_manager.backfill_statistics.call_args[0]
        )
        assert start_date == date(2024, 1, 1)

    async def test_multi_year_range_is_not_capped(
        self, hass: HomeAssistant, mock_coordinator
    ):
        """There is no maximum range: a decade of history reaches the manager
        unchanged. Guards against re-introducing MAX_BACKFILL_DAYS."""
        await async_setup_services(hass)
        requested = date.today() - timedelta(days=365 * 10)

        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKFILL_STATISTICS,
            {"start_date": requested.isoformat()},
            blocking=True,
        )

        start_date, _ = (
            mock_coordinator.statistics_manager.backfill_statistics.call_args[0]
        )
        assert start_date == requested

    async def test_future_start_date_is_rejected(
        self, hass: HomeAssistant, mock_coordinator
    ):
        """A start_date after today is still a validation error."""
        await async_setup_services(hass)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_BACKFILL_STATISTICS,
                {"start_date": tomorrow},
                blocking=True,
            )

        mock_coordinator.statistics_manager.backfill_statistics.assert_not_called()
