"""Conversion between BTCPay's decimal strings and the ledger's integer units.

BTCPay speaks decimal strings ("0.50000000"); the ledger speaks satoshis and
micro-USDT. Every conversion goes through `decimal.Decimal`, and anything that
does not land exactly on a whole unit raises. A deposit that silently rounds is
a deposit that silently loses money.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: Guards against a malformed feed handing us an amount no chain can produce.
#: 21e6 BTC in satoshis; BIGINT still has ~4000x headroom above this.
MAX_UNITS = 2_100_000_000_000_000


class AmountError(ValueError):
    """A monetary string could not be turned into whole smallest units."""


def to_units(value: str | Decimal, decimals: int) -> int:
    """Convert a decimal amount to integer smallest units.

    Raises `AmountError` on anything unparseable, negative, beyond the sanity
    ceiling, or carrying more precision than the asset has.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise AmountError("empty amount")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise AmountError(f"{value!r} is not a decimal number") from exc
    else:
        amount = value

    if not amount.is_finite():
        raise AmountError(f"{value!r} is not a finite amount")
    if amount < 0:
        raise AmountError(f"{value!r} is negative")

    scaled = amount.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise AmountError(
            f"{value!r} has more precision than an asset with {decimals} decimals can hold"
        )

    units = int(scaled)
    if units > MAX_UNITS:
        raise AmountError(f"{value!r} exceeds the maximum representable amount")
    return units


def from_units(units: int, decimals: int) -> str:
    """Render integer smallest units as a fixed-precision decimal string."""
    if units < 0:
        raise AmountError(f"{units} is negative")
    return f"{Decimal(units).scaleb(-decimals):.{decimals}f}"
