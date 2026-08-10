"""Decimal string to integer unit conversion.

Every deposit credit goes through this. Anything it accepts wrongly becomes a
wrong balance, and anything it rounds becomes a small permanent loss.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_processing_api.core.amounts import (
    MAX_UNITS,
    AmountError,
    from_units,
    msat_to_sat_round_up,
    to_units,
)

BTC_DECIMALS = 8
USDT_DECIMALS = 6


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.50000000", 50_000_000),
        ("0.5", 50_000_000),
        ("1", 100_000_000),
        ("0.00000001", 1),
        ("21000000", 2_100_000_000_000_000),
        ("0.00000000", 0),
        ("  0.001  ", 100_000),
        ("1E-8", 1),
        ("1.0E+2", 10_000_000_000),
    ],
)
def test_btc_conversion(value: str, expected: int) -> None:
    assert to_units(value, BTC_DECIMALS) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.000000", 1_000_000), ("0.000001", 1), ("250.5", 250_500_000)],
)
def test_usdt_conversion(value: str, expected: int) -> None:
    assert to_units(value, USDT_DECIMALS) == expected


def test_decimal_input_accepted() -> None:
    assert to_units(Decimal("0.001"), BTC_DECIMALS) == 100_000


def test_sub_satoshi_precision_fails_loudly() -> None:
    """Rounding here would silently shave value off every deposit."""
    with pytest.raises(AmountError, match="more precision"):
        to_units("0.000000001", BTC_DECIMALS)


def test_sub_micro_usdt_precision_fails_loudly() -> None:
    with pytest.raises(AmountError, match="more precision"):
        to_units("0.0000001", USDT_DECIMALS)


@pytest.mark.parametrize("value", ["", "   ", "abc", "0x10", "1,5", "--1", "1.2.3"])
def test_unparseable_values_rejected(value: str) -> None:
    with pytest.raises(AmountError):
        to_units(value, BTC_DECIMALS)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_values_rejected(value: str) -> None:
    with pytest.raises(AmountError, match="finite"):
        to_units(value, BTC_DECIMALS)


def test_negative_rejected() -> None:
    """A negative wallet delta is an outgoing transaction, not a deposit."""
    with pytest.raises(AmountError, match="negative"):
        to_units("-0.5", BTC_DECIMALS)


def test_above_max_supply_rejected() -> None:
    with pytest.raises(AmountError, match="maximum"):
        to_units("21000001", BTC_DECIMALS)


def test_max_supply_exactly_is_allowed() -> None:
    assert to_units("21000000", BTC_DECIMALS) == MAX_UNITS


@pytest.mark.parametrize(
    ("units", "decimals", "expected"),
    [
        (50_000_000, 8, "0.50000000"),
        (1, 8, "0.00000001"),
        (0, 8, "0.00000000"),
        (2_100_000_000_000_000, 8, "21000000.00000000"),
        (1_000_000, 6, "1.000000"),
    ],
)
def test_from_units(units: int, decimals: int, expected: str) -> None:
    assert from_units(units, decimals) == expected


def test_round_trip_is_lossless() -> None:
    for units in (1, 12_345_678, 50_000_000, 2_100_000_000_000_000):
        assert to_units(from_units(units, BTC_DECIMALS), BTC_DECIMALS) == units


def test_from_units_rejects_negative() -> None:
    with pytest.raises(AmountError):
        from_units(-1, BTC_DECIMALS)


@pytest.mark.parametrize(
    ("millisatoshi", "expected"),
    [
        (0, 0),
        (1, 1),
        (999, 1),
        (1000, 1),
        (1001, 2),
        (1500, 2),
        (1999, 2),
        (2000, 2),
        (123_456, 124),
    ],
)
def test_routing_fees_round_up_to_whole_satoshis(millisatoshi: int, expected: int) -> None:
    assert msat_to_sat_round_up(millisatoshi) == expected


def test_rounding_up_never_understates_the_fee() -> None:
    """The direction is the whole reason this function exists.

    Understating a fee leaves satoshis missing from the wallet with no entry
    saying where they went, which is indistinguishable from a loss. Overstating
    costs at most one satoshi and puts it somewhere an operator can read.
    """
    for millisatoshi in range(0, 5000):
        booked = msat_to_sat_round_up(millisatoshi)
        assert booked * 1000 >= millisatoshi
        assert booked * 1000 - millisatoshi < 1000


def test_a_negative_fee_is_refused() -> None:
    with pytest.raises(AmountError, match="negative"):
        msat_to_sat_round_up(-1)
