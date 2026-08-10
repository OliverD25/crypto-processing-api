"""The normalizer, asserted against BTCPay's own bytes.

Every other test of the payout boundary drives `FakeBTCPay`. R3 introduced a
canonical state enum and changed the fake to speak it in the same commit, which
means those tests now prove the fake and the code agree with each other. That
is a real and well-known way for a test suite to keep passing while the thing
it describes drifts away from the server.

These fixtures are raw `response.json()` bodies recorded from a running BTCPay
2.4.2 by `scripts/dev/capture_greenfield.py`. Nothing here constructs a payload
or imports a fake. If BTCPay renames a field or invents a state, this file is
where it shows up.

Re-record with the script on a version bump, and read the diff rather than
accepting it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from crypto_processing_api.gateway.btcpay_models import Invoice, InvoicePaymentMethod, Payout
from crypto_processing_api.services.backends import (
    BackendPayoutState,
    normalize_btcpay_payout,
)
from crypto_processing_api.services.withdrawals import _PAYOUT_STATE_MAP

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "greenfield"

#: What BTCPay 2.4.2 calls each state, and what we call it. Written out rather
#: than imported from the mapping under test — a test that reads its
#: expectations from the code it checks is a tautology.
EXPECTED_STATES: dict[str, BackendPayoutState] = {
    "payout_awaiting_approval": BackendPayoutState.AWAITING_APPROVAL,
    "payout_awaiting_payment": BackendPayoutState.PENDING,
    "payout_in_progress": BackendPayoutState.IN_FLIGHT,
    "payout_completed": BackendPayoutState.COMPLETED,
    "payout_cancelled": BackendPayoutState.CANCELLED,
}


def load(name: str) -> Any:
    path = FIXTURES / f"{name}.json"
    if not path.is_file():
        pytest.fail(
            f"missing fixture {path.name}. Recapture with:\n"
            "  docker compose -f deploy/docker-compose.regtest.yml up -d\n"
            "  python scripts/bootstrap_btcpay.py\n"
            "  python scripts/dev/capture_greenfield.py"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_corpus_is_present() -> None:
    """A deleted corpus must fail loudly, not skip quietly."""
    assert FIXTURES.is_dir(), "tests/fixtures/greenfield/ is gone"
    manifest = load("MANIFEST")
    assert manifest["btcpay_version"] == "2.4.2"
    assert len(manifest["files"]) >= 11


@pytest.mark.parametrize("name", sorted(EXPECTED_STATES))
def test_a_real_payout_normalizes_to_the_canonical_state(name: str) -> None:
    payout = Payout.model_validate(load(name))
    normalized = normalize_btcpay_payout(payout)

    assert normalized.state == EXPECTED_STATES[name], (
        f"{name} carries BTCPay state {payout.state!r}, which normalized to "
        f"{normalized.state!r} instead of {EXPECTED_STATES[name]!r}"
    )
    # The literal survives for the logs, which is the only thing that can tell
    # an operator which BTCPay version grew which state.
    assert normalized.raw_state == payout.state


@pytest.mark.parametrize("name", sorted(EXPECTED_STATES))
def test_every_real_state_reaches_a_withdrawal_status(name: str) -> None:
    """The end-to-end claim: BTCPay's word maps to a status, through the enum."""
    normalized = normalize_btcpay_payout(Payout.model_validate(load(name)))
    assert normalized.state in _PAYOUT_STATE_MAP, (
        f"BTCPay 2.4.2 emits {normalized.raw_state!r} and the state machine has "
        "no status for it, so a payout in that state would be ignored forever"
    )


def test_the_corpus_covers_every_mapped_state() -> None:
    """No mapped state may rest on a payload nobody ever saw BTCPay send."""
    covered = {
        normalize_btcpay_payout(Payout.model_validate(load(name))).state for name in EXPECTED_STATES
    }
    missing = set(_PAYOUT_STATE_MAP) - covered
    assert not missing, (
        f"these states are mapped but have no recorded payload: {sorted(missing)}. "
        "Capture one before trusting the mapping."
    )


def test_an_unrecognised_state_is_unknown_and_therefore_a_no_op() -> None:
    """The safety behaviour, stated as a contract rather than an accident.

    A future BTCPay may invent a state. The correct response is to leave the
    row alone and log it — not to guess, and not to crash the poller for every
    other withdrawal in the batch.

    The payload is a real recorded one with the state field replaced, because
    BTCPay 2.4.2 by definition cannot emit a state it does not have.
    """
    body = dict(load("payout_completed"))
    body["state"] = "SettledOnSomeFutureRail"

    normalized = normalize_btcpay_payout(Payout.model_validate(body))

    assert normalized.state is BackendPayoutState.UNKNOWN
    assert normalized.raw_state == "SettledOnSomeFutureRail"
    assert normalized.state not in _PAYOUT_STATE_MAP


def test_a_real_completed_payout_carries_the_transaction_id() -> None:
    """`paymentProof` is PascalCase and nested; losing it loses the txid."""
    normalized = normalize_btcpay_payout(Payout.model_validate(load("payout_completed")))
    assert normalized.txid, "a Completed payout with no txid means the settle path has nothing"
    assert len(normalized.txid) == 64, f"not a bitcoin txid: {normalized.txid!r}"


def test_a_real_payout_echoes_our_metadata() -> None:
    """The correlation key crash recovery depends on."""
    normalized = normalize_btcpay_payout(Payout.model_validate(load("payout_completed")))
    assert normalized.metadata.get("cpapi") is True
    assert normalized.metadata.get("withdrawal_id"), (
        "without the echoed withdrawal id, a crashed submission cannot be resolved "
        "except by guessing from destination and amount"
    )


def test_a_real_payout_reports_an_amount_the_adoption_path_can_use() -> None:
    normalized = normalize_btcpay_payout(Payout.model_validate(load("payout_completed")))
    assert normalized.amount is not None
    assert float(normalized.amount) > 0


def test_the_payout_list_parses_and_normalizes() -> None:
    """`find_for_withdrawal` reads the list endpoint, not the single GET."""
    payouts = [Payout.model_validate(item) for item in load("payout_list")]
    assert payouts, "the recorded store had no payouts"
    for payout in payouts:
        normalized = normalize_btcpay_payout(payout)
        assert normalized.id
        assert normalized.destination
        assert normalized.state is not BackendPayoutState.UNKNOWN, (
            f"the list endpoint returned state {payout.state!r}, which the "
            "normalizer does not recognise"
        )


# -- Lightning -------------------------------------------------------------
#
# Recorded by the R4 spike against the same 2.4.2 with an LND node attached.
# The claims below are the ones the BTC_LN asset is built on, so each is
# checked against BTCPay's bytes rather than against the design memo.


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ln_payout_awaiting_payment", BackendPayoutState.PENDING),
        ("ln_payout_stuck_awaiting_payment", BackendPayoutState.PENDING),
        ("ln_payout_completed", BackendPayoutState.COMPLETED),
    ],
)
def test_a_lightning_payout_normalizes_through_the_same_states(
    name: str, expected: BackendPayoutState
) -> None:
    """No new canonical state was needed for Lightning.

    That is the load-bearing claim of reusing `BtcpayPayoutBackend`: if a
    Lightning payout reached a state the enum does not have, the withdrawal
    state machine would need a second dialect and the reuse would be a fiction.
    """
    normalized = normalize_btcpay_payout(Payout.model_validate(load(name)))

    assert normalized.state is expected
    assert normalized.state in _PAYOUT_STATE_MAP
    assert normalized.raw_state == load(name)["state"]


def test_a_completed_lightning_payout_reports_the_payment_hash_as_its_txid() -> None:
    """`PayoutLightningBlob` is a different proof shape from the on-chain one.

    It has no `TransactionId`; `Id` carries the 32-byte payment hash. Reading
    it wrongly would confirm a withdrawal with nothing to show the user, and —
    because the hash is 64 hex characters and unique per invoice — it is what
    satisfies the one-txid-per-withdrawal index.
    """
    raw = load("ln_payout_completed")
    assert raw["paymentProof"]["ProofType"] == "PayoutLightningBlob"
    assert raw["paymentProof"]["Id"] == raw["paymentProof"]["PaymentHash"]

    normalized = normalize_btcpay_payout(Payout.model_validate(raw))
    assert normalized.txid == raw["paymentProof"]["PaymentHash"]
    assert len(normalized.txid or "") == 64


def test_a_completed_lightning_payout_does_not_report_the_routing_fee() -> None:
    """The reason fee-drift journaling had to ship with the asset.

    BTCPay reports the payout at its face value in both amount fields, so the
    routing fee the node really paid appears nowhere on the payout. Booked from
    this payload alone, every Lightning withdrawal would leave the channel
    balance a little below what the ledger claims, cumulatively and with no
    entry explaining it — until Job C's insolvency signal fires with nothing an
    operator can act on.

    If a later BTCPay starts reporting a fee here, this test failing is the
    news that the extra lookup can be dropped.
    """
    raw = load("ln_payout_completed")

    assert raw["originalAmount"] == raw["payoutAmount"], (
        "BTCPay now reports a different payout amount from the original; check whether "
        "the difference is the routing fee before trusting the separate lookup"
    )
    assert "feeAmount" not in raw
    assert "fee" not in raw


def test_a_stuck_lightning_payout_carries_no_proof_at_all() -> None:
    """Why the payment hash has to come from the invoice, not the payout.

    A payout that never routed sits in `AwaitingPayment` with `paymentProof:
    null`. The only handle on "did this payment ever leave the node" is the
    payment hash, and the sole place it exists is inside the BOLT11 in
    `destination`.
    """
    raw = load("ln_payout_stuck_awaiting_payment")

    assert raw["paymentProof"] is None
    normalized = normalize_btcpay_payout(Payout.model_validate(raw))
    assert normalized.txid is None
    assert normalized.destination.startswith("lnbcrt")


def test_a_lightning_payout_is_denominated_in_btc() -> None:
    """`amount` reaches `create_payout` as a BTC decimal string for both rails."""
    raw = load("ln_payout_completed")
    assert raw["payoutMethodId"] == "BTC-LN"
    assert raw["payoutCurrency"] == "BTC"
    assert Payout.model_validate(raw).payout_amount == "0.0012"


def test_a_lightning_invoice_offers_a_bolt11_as_its_destination() -> None:
    """`attach_invoice` writes `method.destination` to `deposits.address`.

    For Lightning that is a BOLT11 payment request, not an address — which is
    why `pooled_addresses` is false and why the address-reuse detectors are
    documented as not applying.
    """
    methods = [
        InvoicePaymentMethod.model_validate(m) for m in load("ln_invoice_payment_methods_new")
    ]
    lightning = next(m for m in methods if m.payment_method_id == "BTC-LN")

    assert lightning.destination is not None
    assert lightning.destination.startswith("lnbcrt")
    assert lightning.payment_link == f"lightning:{lightning.destination}"


def test_a_lightning_deposit_is_settled_the_moment_it_is_paid() -> None:
    """No `Processing`, and therefore no confirmation wait.

    The corpus has an `invoice_processing` payload for the on-chain rail and
    deliberately none for Lightning: a settled HTLC has no deeper state to
    reach. `apply_invoice_state` needs no change for it, which is the point.
    """
    invoice = Invoice.model_validate(load("ln_invoice_settled"))
    assert invoice.status == "Settled"
    assert invoice.additional_status == "None"


def test_a_lightning_payment_id_is_the_payment_hash_not_a_txid_vout_pair() -> None:
    """`Payment.txid` splits on the first dash for on-chain ids.

    A Lightning payment id has no dash, so the property returns it unchanged.
    Worth pinning: the unattributed-receive scan matches wallet transactions by
    that prefix, and BTC_LN has no wallet for it to scan.
    """
    methods = [
        InvoicePaymentMethod.model_validate(m) for m in load("ln_invoice_payment_methods_settled")
    ]
    lightning = next(m for m in methods if m.payment_method_id == "BTC-LN")
    payment = lightning.payments[0]

    assert "-" not in payment.id
    assert payment.txid == payment.id
    assert len(payment.id) == 64
    assert payment.status == "Settled"
    assert payment.value == "0.0015"


@pytest.mark.parametrize(
    ("name", "expected_status"),
    [
        ("invoice_new", "New"),
        ("invoice_processing", "Processing"),
        ("invoice_settled", "Settled"),
    ],
)
def test_real_invoices_parse_with_the_status_we_expect(name: str, expected_status: str) -> None:
    invoice = Invoice.model_validate(load(name))
    assert invoice.status == expected_status
    assert invoice.id
    assert invoice.metadata.get("cpapi") is True, "our metadata did not survive the round trip"


def test_a_real_invoice_payment_method_carries_a_destination() -> None:
    """The deposit address comes from here; no destination means no deposit."""
    methods = [InvoicePaymentMethod.model_validate(m) for m in load("invoice_payment_methods_new")]
    assert methods, "the invoice offered no payment methods"
    assert any(m.destination for m in methods)


def test_a_settled_invoice_reports_its_payments() -> None:
    """Amounts are re-read from here rather than trusted from the webhook."""
    methods = [
        InvoicePaymentMethod.model_validate(m) for m in load("invoice_payment_methods_settled")
    ]
    payments = [payment for method in methods for payment in method.payments]
    assert payments, "a settled invoice with no payments would credit nothing"
    assert all(payment.value for payment in payments)
    assert any(payment.status == "Settled" for payment in payments)
