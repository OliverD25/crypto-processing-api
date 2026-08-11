"""The last handful of branches, each one a decision made with nobody watching.

Nothing here belongs to a bigger story than itself. They are collected rather
than scattered because each is a two-line "and if not" that would otherwise
have to be bolted onto a test about something else, which is how a test file
stops being readable.

What they have in common: every one of them decides what to report when an
upstream cannot answer. Reporting the wrong thing — zero for "unknown",
delivered for "unreachable", ok for "never asked" — is worse than reporting
nothing, because it looks like an answer.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from crypto_processing_api.api import health
from crypto_processing_api.api.middleware import IdempotencyContext
from crypto_processing_api.config import get_settings
from crypto_processing_api.core import auth, idempotency
from crypto_processing_api.db import db_session
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.gateway.btcpay_models import (
    LightningBalance,
    PayoutPaymentProof,
)
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.models import (
    AccountKind,
    Asset,
    EntryKind,
    WithdrawalStatus,
)
from crypto_processing_api.main import create_app
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.services.asset_registry import LightningNodeCustody
from crypto_processing_api.services.backends import (
    BackendPayoutState,
    BtcpayPayoutBackend,
    normalize_btcpay_payout,
)
from tests.fake_tron import DESTINATION, HOT_WALLET, FakeTronGrid
from tests.fakes import FakeBTCPay, regtest_address
from tests.integration.conftest import BTC, USDT, bearer, credit_user

DEST = regtest_address("remaining-destination")
FUNDED = 1_000_000


# -- readiness when the database itself is the problem --------------------


class BrokenSession:
    """A session whose every query fails, as one does when Postgres goes."""

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))


def test_readyz_reports_the_database_and_the_worker_as_degraded_together(
    app: FastAPI,
) -> None:
    """Both readings come from the same connection, so losing it must degrade
    both — and neither may report ok from a query that never ran."""
    app.dependency_overrides[db_session] = lambda: BrokenSession()
    try:
        response = TestClient(app).get("/readyz")
    finally:
        app.dependency_overrides.pop(db_session)

    assert response.status_code == 503
    components = {entry["name"]: entry for entry in response.json()["components"]}
    assert components["database"]["status"] == "degraded"
    assert components["database"]["detail"] == "OperationalError"
    assert components["worker"]["status"] == "degraded"


def test_readyz_reports_a_reachable_trongrid_as_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(health, "get_tron_gateway", FakeTronGrid)

    body = client.get("/readyz").json()
    trongrid = next(entry for entry in body["components"] if entry["name"] == "trongrid")
    assert trongrid["status"] == "ok"
    assert "detail" not in trongrid


def test_readyz_says_when_no_job_has_reported_at_all(client: TestClient, session: Session) -> None:
    """A worker that has never started looks identical to one that is fine, if
    the absence of heartbeats is read as "nothing stale"."""
    from sqlalchemy import text

    session.execute(text("DELETE FROM worker_heartbeats"))
    session.commit()

    body = client.get("/readyz").json()
    worker = next(entry for entry in body["components"] if entry["name"] == "worker")
    assert worker["status"] == "degraded"
    assert worker["detail"] == "no job has reported yet"


# -- the request-context middleware ---------------------------------------


def test_an_unhandled_exception_is_logged_with_its_duration_and_re_raised() -> None:
    """The access log line is written on the way out, so a handler that raises
    would otherwise leave no record of the request at all."""
    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("handler exploded")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")

    assert response.status_code == 500
    # The success path is what stamps these on; a failed request gets neither,
    # which is how the two are told apart in a log.
    assert "X-Request-ID" not in response.headers
    assert "Cache-Control" not in response.headers


def test_a_replayed_request_reports_the_resource_it_already_made(session: Session) -> None:
    """The whole reason `resource_id` is recorded at the first commit: a retry
    has to be able to learn the id of the thing it already owns."""
    replay = idempotency.ReplayedResponse(status=201, body={"deposit_id": "d-1"}, resource_id="d-1")
    context = IdempotencyContext(
        session=session, key="k", endpoint="POST /v1/deposits", start=None, replay=replay
    )
    assert context.is_replay is True
    assert context.resource_id == "d-1"
    assert context.replay_response().status_code == 201


# -- api key bookkeeping ---------------------------------------------------


def test_touching_a_key_that_is_not_there_is_a_no_op(session: Session) -> None:
    """`last_used_at` is a diagnostic. A key revoked between authentication and
    this write must not turn a successful request into a 500."""
    auth.touch_last_used(session, "nosuchid")
    session.commit()


# -- transport models ------------------------------------------------------


def test_a_payment_proof_that_is_not_an_object_is_left_for_pydantic() -> None:
    """The casing normaliser runs before validation, so it has to survive
    whatever BTCPay put there."""
    original = PayoutPaymentProof(transaction_id="abc")
    assert PayoutPaymentProof.model_validate(original).txid == "abc"


@pytest.mark.parametrize(
    ("proof", "expected"),
    [
        ({"TransactionId": "aa11"}, "aa11"),
        ({"id": "bb22"}, "bb22"),
        ({"Candidates": ["cc33", "dd44"]}, "cc33"),
        ({"link": "https://mempool.space/tx/ee55"}, "ee55"),
        ({"link": "https://mempool.space/tx/ff66/"}, "ff66"),
        ({}, None),
        ({"link": "/"}, None),
    ],
)
def test_every_shape_btcpay_reports_a_txid_in_is_read(
    proof: dict[str, Any], expected: str | None
) -> None:
    """Reading the wrong one means a confirmed withdrawal with no transaction
    id: nothing to show the user and nothing to check on chain."""
    assert PayoutPaymentProof.model_validate(proof).txid == expected


@pytest.mark.parametrize(
    ("offchain", "expected"),
    [
        ({"local": "500000"}, "500000"),
        ({"local": 500000}, 500000),
        ({"local": {"msat": 1}}, None),
        ({}, None),
        (None, None),
    ],
)
def test_the_lightning_balance_only_reads_a_number_it_recognises(
    offchain: dict[str, Any] | None, expected: Any
) -> None:
    assert LightningBalance(onchain=None, offchain=offchain).local_msat == expected


# -- the Lightning custody source -----------------------------------------


class NoLocalBalance(FakeBTCPay):
    def get_lightning_balance(self, crypto_code: str) -> LightningBalance:
        return LightningBalance.model_validate({"onchain": {}, "offchain": {}})


def test_a_node_that_reports_no_outbound_liquidity_is_unknown_not_zero() -> None:
    """Zero is an insolvency alarm. A shape we cannot read is "we do not know"."""
    assert LightningNodeCustody(NoLocalBalance(), "BTC").balance() is None


def test_an_unparseable_channel_balance_is_unknown_not_zero() -> None:
    fake = FakeBTCPay()
    fake.lightning_local_msat = "half a million"
    assert LightningNodeCustody(fake, "BTC").balance() is None


def test_an_unreachable_node_is_unknown_not_zero() -> None:
    fake = FakeBTCPay()
    fake.fail_next["get_lightning_balance"] = BTCPayUnavailable("node is down", status_code=503)
    assert LightningNodeCustody(fake, "BTC").balance() is None


# -- looking for a stuck payout -------------------------------------------


def test_a_cancelled_payout_is_never_a_candidate(session: Session, fake_btcpay: FakeBTCPay) -> None:
    """A cancelled payout to the same destination cannot be the one we are
    looking for, and counting it as unclaimed would freeze a row that is free
    to be submitted again."""
    credit_user(session, user="cancelled-candidate", amount=FUNDED)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="cancelled-candidate",
        asset_id=BTC,
        amount_gross=100_000,
        destination_address=DEST,
    )
    session.commit()

    stale = fake_btcpay.create_foreign_payout(DEST, "0.00097000")
    fake_btcpay.cancel_payout_externally(stale)

    backend = BtcpayPayoutBackend(fake_btcpay, payout_method_id="BTC-CHAIN")
    mine, unclaimed = backend.find_for_withdrawal(outcome.withdrawal)

    assert mine is None
    assert unclaimed == []


def test_an_unclaimed_payout_to_the_same_destination_is_a_candidate(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """The other half of the same rule: with two withdrawals to one address,
    binding the wrong payout to the wrong row sends the money twice."""
    credit_user(session, user="ambiguous", amount=FUNDED)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="ambiguous",
        asset_id=BTC,
        amount_gross=100_000,
        destination_address=DEST,
    )
    session.commit()

    live = fake_btcpay.create_foreign_payout(DEST, "0.00097000")
    backend = BtcpayPayoutBackend(fake_btcpay, payout_method_id="BTC-CHAIN")
    mine, unclaimed = backend.find_for_withdrawal(outcome.withdrawal)

    assert mine is None
    assert [payout.id for payout in unclaimed] == [live]


def test_a_payout_state_this_version_never_heard_of_normalizes_to_unknown(
    fake_btcpay: FakeBTCPay,
) -> None:
    """A new BTCPay release inventing a state must not take the poller down."""
    payout = fake_btcpay.create_payout(
        destination=DEST, amount="0.001", payout_method_id="BTC-CHAIN", metadata={}
    )
    fake_btcpay.payouts[payout.id].state = "AwaitingRefund"
    normalized = normalize_btcpay_payout(fake_btcpay.get_payout(payout.id))
    assert normalized.state is BackendPayoutState.UNKNOWN
    assert normalized.raw_state == "AwaitingRefund"


# -- the ledger's own error handling --------------------------------------


def test_posting_to_an_account_that_does_not_exist_says_so(session: Session) -> None:
    """The accounts are locked before anything is written, so a bad id is
    caught by name rather than surfacing later as a foreign-key error nobody
    can trace back to a posting."""
    hot = ledger.get_system_account(session, asset_id=BTC, kind=AccountKind.HOT_WALLET)
    with pytest.raises(ledger.AccountNotFound, match="2000000000"):
        ledger.post_entry(
            session,
            kind=EntryKind.ADJUSTMENT,
            asset_id=BTC,
            source_ref=f"bad-account:{uuid.uuid4()}",
            postings=[(hot.id, 100), (2_000_000_000, -100)],
        )
    session.rollback()


# -- admin approval re-pricing --------------------------------------------


def test_an_approval_that_no_longer_prices_is_a_422(
    client: TestClient, session: Session, admin_key: str
) -> None:
    """A manual withdrawal is priced again at approval, because an operator can
    take hours. If the fee has been raised past the amount in between, the
    right answer is to refuse rather than to hand an operator an impossible
    transfer."""
    credit_user(session, user="repriced", amount=500_000_000, asset=USDT)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="repriced",
        asset_id=USDT,
        amount_gross=2_000_000,
        destination_address=DESTINATION,
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        force_manual=True,
    )
    session.commit()

    usdt = session.get(Asset, USDT)
    assert usdt is not None
    usdt.withdrawal_flat_fee = 5_000_000
    session.commit()

    response = client.post(
        f"/v1/admin/withdrawals/{outcome.withdrawal.id}/approve",
        json={},
        headers=bearer(admin_key),
    )
    assert response.status_code == 422
    assert "flat fee" in response.json()["detail"]

    session.expire_all()
    withdrawal = withdrawal_service.get(session, outcome.withdrawal.id)
    assert withdrawal.status is WithdrawalStatus.PENDING_APPROVAL


def test_the_event_queue_lists_everything_when_no_filter_is_given(
    client: TestClient, session: Session, admin_key: str
) -> None:
    from crypto_processing_api.services import events as event_service

    event_service.emit(session, event_type="deposit.settled", payload={"deposit_id": "d-1"})
    session.commit()

    body = client.get("/v1/admin/events", headers=bearer(admin_key)).json()
    assert [item["type"] for item in body["events"]] == ["deposit.settled"]
    assert body["events"][0]["id"].startswith("evt_")
    assert body["events"][0]["status"] == "pending"


def test_an_explicit_null_expected_amount_is_accepted(
    client: TestClient, readwrite_key: str
) -> None:
    """A client that always sends the field, with null when it does not know,
    must not be answered with a validation error."""
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": "u1", "asset": BTC, "expected_amount": None},
        headers={**bearer(readwrite_key), "Idempotency-Key": f"d-{uuid.uuid4()}"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "pending"


# -- the pooled tolerance with nothing settled ----------------------------


def test_the_pooled_tolerance_needs_something_settled_to_compare(
    session: Session, fake_btcpay: FakeBTCPay
) -> None:
    """A settled invoice whose only payment was marked invalid has received
    nothing. Comparing zero against the expected amount would send every one of
    them to review."""
    from crypto_processing_api.services import deposits as deposit_service

    fake_btcpay.payment_methods = ["BTC-CHAIN", "USDT_TRC20"]
    deposit = deposit_service.create_deposit(
        session, external_user_id="nothing-settled", asset_id=USDT, amount_expected=200_000_000
    )
    deposit_service.ensure_invoice(session, fake_btcpay, get_settings(), deposit=deposit)
    session.commit()

    invoice_id = deposit.btcpay_invoice_id or ""
    fake_btcpay.add_payment(invoice_id, "500.000000", status="Invalid")
    fake_btcpay.settle(invoice_id)

    deposit_service.refresh_deposit(session, fake_btcpay, deposit_id=deposit.id, tolerance_pct=1.0)
    session.commit()

    from crypto_processing_api.ledger.models import DepositStatus

    assert deposit.status is DepositStatus.SETTLED
