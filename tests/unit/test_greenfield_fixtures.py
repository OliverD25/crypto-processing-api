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
