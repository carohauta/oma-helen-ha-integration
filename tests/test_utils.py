"""Tests for the Helen Energy utility helpers."""

import pytest

from custom_components.helen_energy.const import (
    CONTRACT_TYPE_AUTOMATIC,
    CONTRACT_TYPE_EXCHANGE,
    CONTRACT_TYPE_FIXED,
    CONTRACT_TYPE_MARKET,
)
from custom_components.helen_energy.utils import resolve_contract_type


class TestResolveContractType:
    """Test resolve_contract_type."""

    @pytest.mark.parametrize(
        "user_choice",
        [CONTRACT_TYPE_FIXED, CONTRACT_TYPE_MARKET, CONTRACT_TYPE_EXCHANGE],
    )
    def test_concrete_user_choice_wins_over_api(self, user_choice):
        """An explicit user choice is honoured regardless of the API contract code."""
        assert resolve_contract_type(user_choice, "PERUSSOPIMUS") == user_choice
        assert resolve_contract_type(user_choice, "MARKKINASAHKO") == user_choice
        assert resolve_contract_type(user_choice, "PORSSISAHKO") == user_choice
        assert resolve_contract_type(user_choice, None) == user_choice

    @pytest.mark.parametrize(
        "api_code, expected",
        [
            ("PERUSSOPIMUS", CONTRACT_TYPE_FIXED),
            ("KAYTTOSOPIMUS", CONTRACT_TYPE_FIXED),
            ("MARKKINASAHKO", CONTRACT_TYPE_MARKET),
            ("PORSSISAHKO", CONTRACT_TYPE_EXCHANGE),
        ],
    )
    def test_automatic_derives_from_api(self, api_code, expected):
        """Automatic mode picks the type from the API contract code."""
        assert resolve_contract_type(CONTRACT_TYPE_AUTOMATIC, api_code) == expected

    def test_automatic_unknown_api_defaults_to_fixed(self):
        """Unrecognised API codes fall back to fixed."""
        assert (
            resolve_contract_type(CONTRACT_TYPE_AUTOMATIC, "GIBBERISH")
            == CONTRACT_TYPE_FIXED
        )

    def test_automatic_missing_api_defaults_to_fixed(self):
        """Missing API contract code falls back to fixed."""
        assert (
            resolve_contract_type(CONTRACT_TYPE_AUTOMATIC, None) == CONTRACT_TYPE_FIXED
        )
        assert (
            resolve_contract_type(CONTRACT_TYPE_AUTOMATIC, "") == CONTRACT_TYPE_FIXED
        )

    def test_none_user_choice_derives_from_api(self):
        """Missing user choice (legacy entries) behaves like automatic."""
        assert resolve_contract_type(None, "MARKKINASAHKO") == CONTRACT_TYPE_MARKET
        assert resolve_contract_type(None, None) == CONTRACT_TYPE_FIXED
