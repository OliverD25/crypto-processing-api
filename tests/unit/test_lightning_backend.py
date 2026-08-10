"""The two things `LightningPayoutBackend` adds to the on-chain one.

Everything else it does is inherited verbatim and is covered by the conformance
run in `tests/integration/test_backend_contracts.py`. What is here is the pair
of questions only a Lightning node can answer: what the route cost, and whether
the payment ever left.

The destinations are real BOLT11 invoices from the committed corpus, so the
payment hash these tests key on is the one BTCPay reported for the same payment
rather than one this file made up.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto_processing_api.core.addresses import decode_bolt11
from crypto_processing_api.gateway.btcpay_client import BTCPayNotFound, BTCPayUnavailable
from crypto_processing_api.services.backends import (
    BackendPayout,
    BackendPayoutState,
    LightningPayoutBackend,
)
from tests.fakes import FakeBTCPay

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "greenfield"
SAMPLES = json.loads((FIXTURES / "ln_bolt11_samples.json").read_text(encoding="utf-8"))
BOLT11 = SAMPLES["payout_completed"]
PAYMENT_HASH = decode_bolt11(BOLT11, network="regtest").payment_hash


@pytest.fixture
def fake() -> FakeBTCPay:
    return FakeBTCPay()


@pytest.fixture
def backend(fake: FakeBTCPay) -> LightningPayoutBackend:
    return LightningPayoutBackend(
        fake, payout_method_id="BTC-LN", crypto_code="BTC", network="regtest"
    )


def payout(state: BackendPayoutState = BackendPayoutState.COMPLETED) -> BackendPayout:
    return BackendPayout(
        id="payout-ln-1", state=state, destination=BOLT11, amount="0.0012", raw_state="Completed"
    )


# -- the routing fee -------------------------------------------------------


def test_the_payment_hash_comes_from_the_invoice(backend: LightningPayoutBackend) -> None:
    """Not from the payout, because a stuck payout has no proof to read."""
    assert backend.payment_hash(payout(BackendPayoutState.PENDING)) == PAYMENT_HASH


def test_the_routing_fee_is_read_in_millisatoshi_and_rounded_up(
    backend: LightningPayoutBackend, fake: FakeBTCPay
) -> None:
    """1500 msat is a satoshi and a half of real money already gone."""
    fake.record_lightning_payment(PAYMENT_HASH, total_msat=120_001_500, fee_msat=1_500)
    assert backend.actual_wallet_fee(payout()) == 2


def test_a_zero_fee_is_zero_and_not_missing(
    backend: LightningPayoutBackend, fake: FakeBTCPay
) -> None:
    """A direct-channel payment routes for nothing, and that is a real answer."""
    fake.record_lightning_payment(PAYMENT_HASH, total_msat=120_000_000, fee_msat=0)
    assert backend.actual_wallet_fee(payout()) == 0


def test_a_payment_the_node_reports_without_a_fee_gives_none(
    backend: LightningPayoutBackend, fake: FakeBTCPay
) -> None:
    """None means "book the estimate", which is different from booking zero."""
    fake.record_lightning_payment(PAYMENT_HASH, total_msat=120_000_000, fee_msat=None)
    assert backend.actual_wallet_fee(payout()) is None


def test_an_unreachable_node_raises_rather_than_reporting_no_fee(
    backend: LightningPayoutBackend, fake: FakeBTCPay
) -> None:
    """The caller has to be able to tell an outage from a fee-less rail.

    Swallowing this into None would book the estimate silently, and on
    Lightning the estimate is zero — so a node outage would look exactly like a
    string of free payments.
    """
    fake.fail_next["get_lightning_payment"] = BTCPayUnavailable("node is down")
    with pytest.raises(BTCPayUnavailable):
        backend.actual_wallet_fee(payout())


# -- the definitive-failure proof ------------------------------------------


def test_a_node_with_no_record_of_the_payment_is_proof(
    backend: LightningPayoutBackend, fake: FakeBTCPay
) -> None:
    proof = backend.definitive_failure_proof(payout(BackendPayoutState.CANCELLED))

    assert proof is not None
    assert PAYMENT_HASH in proof
    assert "did not leave" in proof


def test_a_failed_payment_is_proof(backend: LightningPayoutBackend, fake: FakeBTCPay) -> None:
    """Every route attempt failed, so no HTLC can still settle."""
    fake.record_lightning_payment(PAYMENT_HASH, status="Failed")
    proof = backend.definitive_failure_proof(payout(BackendPayoutState.CANCELLED))

    assert proof is not None
    assert "Failed" in proof


@pytest.mark.parametrize("status", ["Pending", "Complete", "Unknown", "SomeFutureState"])
def test_anything_short_of_certainty_is_not_proof(
    backend: LightningPayoutBackend, fake: FakeBTCPay, status: str
) -> None:
    """The list of definitive statuses is an allow-list on purpose.

    `Pending` may still settle and `Complete` already has. `Unknown` is the
    node declining to say, which is exactly when a human should look. A future
    status this version has never heard of gets the same treatment as Unknown,
    because guessing about it would be guessing about money.
    """
    fake.record_lightning_payment(PAYMENT_HASH, status=status)
    assert backend.definitive_failure_proof(payout(BackendPayoutState.CANCELLED)) is None


def test_an_unreachable_node_is_not_proof(
    backend: LightningPayoutBackend, fake: FakeBTCPay
) -> None:
    fake.fail_next["get_lightning_payment"] = BTCPayUnavailable("node is down")
    assert backend.definitive_failure_proof(payout(BackendPayoutState.CANCELLED)) is None


def test_a_404_from_a_store_with_no_lightning_node_is_not_proof(
    backend: LightningPayoutBackend, fake: FakeBTCPay
) -> None:
    """The failure this guard exists for.

    A store with Lightning misconfigured answers 404 for every payment hash,
    which without the check would read as "no payment ever left" for every
    withdrawal at once — and release every hold in the queue. So the node has
    to answer something before its silence counts as evidence.
    """
    fake.fail_next["get_lightning_balance"] = BTCPayNotFound("this store has no lightning node")
    assert backend.definitive_failure_proof(payout(BackendPayoutState.CANCELLED)) is None


def test_a_destination_that_is_not_an_invoice_answers_nothing(
    backend: LightningPayoutBackend,
) -> None:
    """Unreachable unless BTCPay echoes back something we never sent, but the
    answer still has to be "no proof" rather than an exception in a worker."""
    broken = BackendPayout(
        id="payout-ln-2",
        state=BackendPayoutState.CANCELLED,
        destination="bcrt1qkfycfk4zd2c907ychq502p5seze67nsny0kqxq",
    )

    assert backend.payment_hash(broken) is None
    assert backend.definitive_failure_proof(broken) is None
    assert backend.actual_wallet_fee(broken) is None
