"""Fee arithmetic and the fallback chain."""

from __future__ import annotations

from typing import Any

import pytest

from crypto_processing_api.config import Settings
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.services import fees
from tests.fakes import FakeBTCPay

BTC_METHOD = "BTC-CHAIN"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://cpapi:cpapi@localhost:5432/cpapi",
        "environment": "development",
        "mempool_space_url": None,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


class RateGateway(FakeBTCPay):
    """FakeBTCPay with a controllable fee rate."""

    def __init__(self, rate: float | None = 10.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rate = rate

    def get_fee_rate(self, payment_method_id: str, *, block_target: int) -> float:
        self._maybe_fail("get_fee_rate")
        if self.rate is None:
            raise BTCPayUnavailable("no estimate")
        return self.rate


def test_fee_is_rate_times_assumed_vsize() -> None:
    quote = fees.quote_btc_fee(
        RateGateway(10.0),
        make_settings(btc_payout_vsize_vb=300),
        gross=1_000_000,
        payment_method_id=BTC_METHOD,
    )
    assert quote.fee == 3_000
    assert quote.net == 997_000
    assert quote.source == "btcpay"


def test_vsize_default_is_above_the_two_hundred_the_review_called_too_low() -> None:
    """A deposit wallet is a pile of small UTXOs; 200 vB systematically underprices."""
    assert make_settings().btc_payout_vsize_vb == 300


def test_fractional_rates_round_up() -> None:
    """Rounding down would leave the operator paying the remainder every time."""
    quote = fees.quote_btc_fee(
        RateGateway(1.5),
        make_settings(btc_payout_vsize_vb=333),
        gross=1_000_000,
        payment_method_id=BTC_METHOD,
    )
    assert quote.fee == 500  # 1.5 * 333 = 499.5


def test_committed_is_what_leaves_the_wallet() -> None:
    quote = fees.quote_btc_fee(
        RateGateway(10.0),
        make_settings(btc_payout_vsize_vb=300),
        gross=1_000_000,
        payment_method_id=BTC_METHOD,
    )
    assert quote.committed == quote.net + quote.wallet_fee == 1_000_000


def test_absorb_mode_gives_the_user_the_whole_amount() -> None:
    quote = fees.quote_btc_fee(
        RateGateway(10.0),
        make_settings(withdrawal_fee_mode="absorb", btc_payout_vsize_vb=300),
        gross=1_000_000,
        payment_method_id=BTC_METHOD,
    )
    assert quote.fee == 0
    assert quote.net == 1_000_000
    # The operator still pays the miner, so more than the amount leaves.
    assert quote.wallet_fee == 3_000
    assert quote.committed == 1_003_000


def test_dust_is_refused() -> None:
    with pytest.raises(fees.DustAmount, match="dust"):
        fees.quote_btc_fee(
            RateGateway(10.0),
            make_settings(btc_payout_vsize_vb=300, btc_dust_threshold_sat=546),
            gross=3_400,
            payment_method_id=BTC_METHOD,
        )


def test_fee_larger_than_the_amount_is_refused() -> None:
    with pytest.raises(fees.DustAmount, match="not smaller than"):
        fees.quote_btc_fee(
            RateGateway(100.0),
            make_settings(btc_payout_vsize_vb=300),
            gross=1_000,
            payment_method_id=BTC_METHOD,
        )


def test_exactly_the_dust_threshold_is_refused() -> None:
    """546 is unspendable, so the boundary is inclusive."""
    with pytest.raises(fees.DustAmount):
        fees.quote_btc_fee(
            RateGateway(1.0),
            make_settings(btc_payout_vsize_vb=100, btc_dust_threshold_sat=546),
            gross=646,
            payment_method_id=BTC_METHOD,
        )


# -- the fallback chain ----------------------------------------------------


def test_btcpay_is_preferred() -> None:
    rate, source = fees.estimate_sat_per_vb(
        RateGateway(7.0), make_settings(), payment_method_id=BTC_METHOD
    )
    assert (rate, source) == (7.0, "btcpay")


def test_falls_through_to_static_when_nothing_answers() -> None:
    rate, source = fees.estimate_sat_per_vb(
        RateGateway(None),
        make_settings(btc_fallback_fee_sat_per_vb=25, mempool_space_url=None),
        payment_method_id=BTC_METHOD,
    )
    assert (rate, source) == (25.0, "static")


def test_a_nonsense_rate_from_btcpay_is_not_used() -> None:
    """Zero would price every withdrawal at no fee at all."""
    rate, source = fees.estimate_sat_per_vb(
        RateGateway(0.0),
        make_settings(btc_fallback_fee_sat_per_vb=25, mempool_space_url=None),
        payment_method_id=BTC_METHOD,
    )
    assert source == "static"
    assert rate == 25.0


def test_mempool_space_is_used_when_btcpay_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, int]:
            return {"fastestFee": 40, "halfHourFee": 30, "hourFee": 20}

    monkeypatch.setattr(fees.httpx, "get", lambda *a, **k: Response())
    rate, source = fees.estimate_sat_per_vb(
        RateGateway(None),
        make_settings(mempool_space_url="https://mempool.test/fees"),
        payment_method_id=BTC_METHOD,
    )
    assert (rate, source) == (30.0, "mempool.space")


def test_mempool_failure_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise fees.httpx.ConnectError("no route")

    monkeypatch.setattr(fees.httpx, "get", explode)
    _rate, source = fees.estimate_sat_per_vb(
        RateGateway(None),
        make_settings(mempool_space_url="https://mempool.test/fees"),
        payment_method_id=BTC_METHOD,
    )
    assert source == "static"


# -- flat fees (USDT, M4) --------------------------------------------------


def test_flat_fee_quote() -> None:
    quote = fees.flat_fee_quote(gross=200_000_000, flat_fee=1_000_000)
    assert quote.fee == 1_000_000
    assert quote.net == 199_000_000
    # TRC-20 gas is paid in TRX, not in the asset, so the wallet spends only net.
    assert quote.wallet_fee == 0
    assert quote.committed == 199_000_000


def test_flat_fee_larger_than_the_amount_is_refused() -> None:
    with pytest.raises(fees.DustAmount):
        fees.flat_fee_quote(gross=500_000, flat_fee=1_000_000)


def test_a_flat_fee_that_leaves_only_dust_is_refused() -> None:
    """The fee being smaller than the amount is not enough on its own: what
    reaches the destination still has to be worth sending."""
    with pytest.raises(fees.DustAmount, match="only 400 units"):
        fees.flat_fee_quote(gross=1_400, flat_fee=1_000, dust_threshold=546)


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"halfHourFee": 0, "hourFee": 0}, "zero would price every payout at nothing"),
        ({"halfHourFee": -5}, "a negative rate is not a rate"),
        ({"halfHourFee": "thirty"}, "a string where a number belongs"),
        ({}, "the shape changed and the field is gone"),
    ],
)
def test_a_nonsense_rate_from_mempool_space_falls_through(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], why: str
) -> None:
    """A third party on a free endpoint is not trusted to be sane. Reaching the
    static floor is loud in the logs; using a zero rate would be silent."""

    class Response:
        status_code = 200

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return payload

    monkeypatch.setattr(fees.httpx, "get", lambda *a, **k: Response())
    rate, source = fees.estimate_sat_per_vb(
        RateGateway(None),
        make_settings(
            mempool_space_url="https://mempool.test/fees", btc_fallback_fee_sat_per_vb=20
        ),
        payment_method_id=BTC_METHOD,
    )
    assert (rate, source) == (20.0, "static"), why


def test_the_hour_rate_is_used_when_the_half_hour_one_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {"hourFee": 12}

    monkeypatch.setattr(fees.httpx, "get", lambda *a, **k: Response())
    rate, source = fees.estimate_sat_per_vb(
        RateGateway(None),
        make_settings(mempool_space_url="https://mempool.test/fees"),
        payment_method_id=BTC_METHOD,
    )
    assert (rate, source) == (12.0, "mempool.space")
