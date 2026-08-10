"""Submission, confirmation, the posting matrix, and the crash window."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    EntryKind,
    JournalEntry,
    OutboundEvent,
    WithdrawalStatus,
)
from crypto_processing_api.services import events as event_service
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.workers import payout_submitter, reconciliation, webhook_processor
from tests.fakes import FakeBTCPay, regtest_address
from tests.integration.conftest import BTC, bearer, credit_user, post_webhook

DEST = regtest_address("withdrawal-destination")
GROSS = 100_000
FEE = 3_000  # FakeBTCPay quotes 10 sat/vB, default assumed size 300 vB
NET = GROSS - FEE
FUNDED = 1_000_000


def user_balance(session: Session, user: str, kind: AccountKind) -> int:
    session.rollback()
    account = session.execute(
        select(Account).where(
            Account.asset_id == BTC,
            Account.kind == kind,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


def system_balance(session: Session, kind: AccountKind) -> int:
    session.rollback()
    return ledger.get_system_account(session, asset_id=BTC, kind=kind).balance


def make_withdrawal(
    client: TestClient, key: str, *, user: str, amount: int = GROSS, destination: str = DEST
) -> uuid.UUID:
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": user,
            "asset": BTC,
            "amount": str(amount),
            "destination_address": destination,
        },
        headers={**bearer(key), "Idempotency-Key": f"w-{uuid.uuid4()}"},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["withdrawal_id"])


def submit(
    session_factory: sessionmaker[Session], fake: FakeBTCPay
) -> payout_submitter.SubmitReport:
    return payout_submitter.submit_approved(session_factory, fake, get_settings())


# -- the posting matrix ----------------------------------------------------


def test_each_stage_posts_what_the_matrix_says(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """Balances after every stage, against the documented matrix."""
    credit_user(session, user="matrix", amount=FUNDED)
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED

    withdrawal_id = make_withdrawal(client, readwrite_key, user="matrix")

    # after hold: DR user_available +G / CR user_hold -G
    assert user_balance(session, "matrix", AccountKind.USER_AVAILABLE) == FUNDED - GROSS
    assert user_balance(session, "matrix", AccountKind.USER_HOLD) == GROSS
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0

    report = submit(session_factory, fake_btcpay)
    assert report.submitted == 1

    # after submit: DR payouts_in_flight +C / CR hot_wallet -C, C = net + fee
    committed = NET + FEE
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == committed
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED - committed
    assert user_balance(session, "matrix", AccountKind.USER_HOLD) == GROSS

    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    # after settle: hold extinguished, in-flight cleared, fee visible
    assert user_balance(session, "matrix", AccountKind.USER_HOLD) == 0
    assert user_balance(session, "matrix", AccountKind.USER_AVAILABLE) == FUNDED - GROSS
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED - GROSS
    assert system_balance(session, AccountKind.NETWORK_FEE_EXPENSE) == FEE
    assert system_balance(session, AccountKind.FEE_INCOME) == -FEE

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED
    assert withdrawal.fee_amount == FEE
    assert withdrawal.amount_net == NET
    assert withdrawal.txid
    assert_ledger_consistent(session)


def test_absorb_mode_commits_more_than_the_user_pays(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under absorb the user receives the full amount and the operator pays the miner."""
    settings = get_settings()
    monkeypatch.setattr(settings, "withdrawal_fee_mode", "absorb")

    credit_user(session, user="absorbed", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="absorbed")
    submit(session_factory, fake_btcpay)

    committed = GROSS + FEE
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == committed

    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    assert withdrawal.fee_amount == 0
    assert withdrawal.amount_net == GROSS
    # The operator carried the miner fee, so the wallet is down more than the
    # user was charged and fee_income earned nothing.
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED - committed
    assert system_balance(session, AccountKind.NETWORK_FEE_EXPENSE) == FEE
    assert system_balance(session, AccountKind.FEE_INCOME) == 0
    assert_ledger_consistent(session)


def test_the_payout_is_created_for_the_net_amount(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """BTCPay pays the miner on top, so the payout must be net, not gross."""
    credit_user(session, user="netpay", amount=FUNDED)
    make_withdrawal(client, readwrite_key, user="netpay")
    submit(session_factory, fake_btcpay)

    payout = next(iter(fake_btcpay.payouts.values()))
    assert payout.amount == "0.00097000"
    assert payout.destination == DEST
    assert payout.metadata["cpapi"] is True


# -- submission races and the crash window --------------------------------


def test_two_workers_cannot_submit_the_same_withdrawal(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    credit_user(session, user="double", amount=FUNDED)
    make_withdrawal(client, readwrite_key, user="double")

    barrier = threading.Barrier(2)

    def worker(_index: int) -> int:
        barrier.wait(timeout=30)
        return submit(session_factory, fake_btcpay).submitted

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=60) for f in [pool.submit(worker, i) for i in range(2)]]

    assert sum(results) == 1
    assert len(fake_btcpay.payouts) == 1
    assert_ledger_consistent(session)


def test_an_ambiguous_submission_leaves_the_row_in_submitting(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """Never failed and never retried: the payout may exist."""
    credit_user(session, user="ambiguous", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="ambiguous")

    fake_btcpay.fail_next["create_payout"] = BTCPayUnavailable("timeout")
    submit(session_factory, fake_btcpay)

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.SUBMITTING
    assert withdrawal.backend_ref is None
    # No in-flight entry either: nothing is known to have been committed.
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0


def test_stuck_submitting_adopts_the_payout_that_carries_its_id(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """The crash drill. This is what the metadata echo buys."""
    credit_user(session, user="crashed", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="crashed")

    # BTCPay created the payout; the process died before writing it down.
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    fake_btcpay.create_payout(
        destination=DEST,
        amount="0.00097000",
        payout_method_id="BTC-CHAIN",
        metadata={"cpapi": True, "withdrawal_id": str(withdrawal_id)},
    )
    session.execute(
        text("UPDATE withdrawals SET status = 'submitting', updated_at = now() - interval '1 hour'")
    )
    session.commit()

    report = payout_submitter.resolve_stuck(session_factory, fake_btcpay, get_settings())
    assert report.adopted == 1

    session.rollback()
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.SUBMITTED
    assert withdrawal.backend_ref is not None
    assert len(fake_btcpay.payouts) == 1
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == NET + FEE
    assert_ledger_consistent(session)


def test_stuck_submitting_returns_to_approved_when_nothing_was_created(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    credit_user(session, user="nothing", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="nothing")
    session.execute(
        text("UPDATE withdrawals SET status = 'submitting', updated_at = now() - interval '1 hour'")
    )
    session.commit()

    report = payout_submitter.resolve_stuck(session_factory, fake_btcpay, get_settings())
    assert report.resubmittable == 1

    session.rollback()
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.APPROVED


def test_an_unclaimed_payout_to_the_same_destination_freezes_the_row(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """Critique #6, the case that would otherwise double-send.

    A payout to this destination exists with no withdrawal id we can read. It
    might be ours. "No match for this row" is not evidence that nothing was
    sent, so nothing is resubmitted and a human is asked.
    """
    credit_user(session, user="frozen", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="frozen")
    fake_btcpay.create_foreign_payout(DEST, "0.00097000")
    session.execute(
        text("UPDATE withdrawals SET status = 'submitting', updated_at = now() - interval '1 hour'")
    )
    session.commit()

    report = payout_submitter.resolve_stuck(session_factory, fake_btcpay, get_settings())
    assert report.frozen == 1
    assert report.resubmittable == 0

    session.rollback()
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.SUBMITTING
    assert "an admin must decide" in (withdrawal.failure_reason or "")


# -- payout state machine --------------------------------------------------


def test_payout_webhook_drives_broadcast_and_confirmation(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    credit_user(session, user="webhooked", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="webhooked")
    submit(session_factory, fake_btcpay)
    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id

    txid = fake_btcpay.broadcast_payout(payout_id)
    post_webhook(client, fake_btcpay, fake_btcpay.payout_webhook("PayoutUpdated", payout_id))
    webhook_processor.process_pending(session_factory, fake_btcpay)

    session.rollback()
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.BROADCAST
    # The txid comes from the follow-up GET, not from the webhook payload.
    assert withdrawal.txid == txid

    fake_btcpay.complete_payout(payout_id)
    post_webhook(client, fake_btcpay, fake_btcpay.payout_webhook("PayoutUpdated", payout_id))
    webhook_processor.process_pending(session_factory, fake_btcpay)

    session.rollback()
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED
    assert_ledger_consistent(session)


def test_an_out_of_order_event_is_a_no_op(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    credit_user(session, user="ooo", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="ooo")
    submit(session_factory, fake_btcpay)
    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id

    fake_btcpay.complete_payout(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    # An InProgress arriving after Completed must not walk the row backwards.
    fake_btcpay.payouts[payout_id].state = "InProgress"
    with session_factory() as worker:
        withdrawal_service.apply_payout_state(
            worker, withdrawal_id=withdrawal_id, payout=fake_btcpay.get_payout(payout_id)
        )
        worker.commit()

    session.rollback()
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.CONFIRMED
    assert (
        session.execute(
            select(func.count())
            .select_from(JournalEntry)
            .where(JournalEntry.kind == EntryKind.WITHDRAWAL_SETTLE)
        ).scalar_one()
        == 1
    )


def test_settling_twice_credits_once(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    credit_user(session, user="twice", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="twice")
    submit(session_factory, fake_btcpay)
    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)

    for _ in range(4):
        reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED - GROSS
    assert system_balance(session, AccountKind.NETWORK_FEE_EXPENSE) == FEE
    assert_ledger_consistent(session)


def test_a_cancelled_payout_fails_but_never_auto_releases(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
) -> None:
    """Critique #2. A cancelled payout may still have been broadcast."""
    credit_user(session, user="cancelled", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="cancelled")
    submit(session_factory, fake_btcpay)
    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id

    fake_btcpay.cancel_payout_externally(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    session.rollback()
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.status == WithdrawalStatus.FAILED
    # The money stays held until a human says where it went.
    assert user_balance(session, "cancelled", AccountKind.USER_HOLD) == GROSS
    assert user_balance(session, "cancelled", AccountKind.USER_AVAILABLE) == FUNDED - GROSS


# -- admin actions ---------------------------------------------------------


def test_approve_then_submit(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
    admin_key: str,
) -> None:
    credit_user(session, user="queued", amount=10_000_000)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="queued", amount=600_000)

    queue = client.get(
        "/v1/admin/withdrawals?status=pending_approval", headers=bearer(admin_key)
    ).json()
    assert [w["withdrawal_id"] for w in queue["withdrawals"]] == [str(withdrawal_id)]

    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/approve", json={}, headers=bearer(admin_key)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    assert submit(session_factory, fake_btcpay).submitted == 1


def test_approving_twice_is_a_conflict(
    client: TestClient, session: Session, readwrite_key: str, admin_key: str
) -> None:
    credit_user(session, user="doubleapprove", amount=10_000_000)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="doubleapprove", amount=600_000)
    client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/approve", json={}, headers=bearer(admin_key)
    )
    second = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/approve", json={}, headers=bearer(admin_key)
    )
    assert second.status_code == 409


def test_reject_refunds_exactly(
    client: TestClient, session: Session, readwrite_key: str, admin_key: str
) -> None:
    credit_user(session, user="rejected", amount=10_000_000)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="rejected", amount=600_000)

    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/reject",
        json={"reason": "suspicious destination"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "refunded"

    assert user_balance(session, "rejected", AccountKind.USER_AVAILABLE) == 10_000_000
    assert user_balance(session, "rejected", AccountKind.USER_HOLD) == 0
    assert_ledger_consistent(session)


def test_release_after_submission_requires_an_attestation(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
    admin_key: str,
) -> None:
    credit_user(session, user="attested", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="attested")
    submit(session_factory, fake_btcpay)

    empty = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/release",
        json={"attestation": ""},
        headers=bearer(admin_key),
    )
    assert empty.status_code == 422

    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/release",
        json={"attestation": "txid verified never broadcast, checked mempool and chain"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "refunded"

    # The hold came back and the in-flight commitment was unwound.
    assert user_balance(session, "attested", AccountKind.USER_AVAILABLE) == FUNDED
    assert user_balance(session, "attested", AccountKind.USER_HOLD) == 0
    assert system_balance(session, AccountKind.PAYOUTS_IN_FLIGHT) == 0
    assert system_balance(session, AccountKind.HOT_WALLET) == FUNDED

    withdrawal = withdrawal_service.get(session, withdrawal_id)
    session.refresh(withdrawal)
    assert withdrawal.release_attestation
    assert withdrawal.released_by
    assert_ledger_consistent(session)


def test_a_confirmed_withdrawal_can_never_be_released(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
    admin_key: str,
) -> None:
    credit_user(session, user="final", amount=FUNDED)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="final")
    submit(session_factory, fake_btcpay)
    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.complete_payout(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    response = client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/release",
        json={"attestation": "I would like the money back please"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 409
    assert user_balance(session, "final", AccountKind.USER_AVAILABLE) == FUNDED - GROSS


def test_admin_endpoints_need_admin_scope(
    client: TestClient, session: Session, readwrite_key: str
) -> None:
    credit_user(session, user="scoped", amount=10_000_000)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="scoped", amount=600_000)
    for path, body in (
        (f"/v1/admin/withdrawals/{withdrawal_id}/approve", {}),
        (f"/v1/admin/withdrawals/{withdrawal_id}/reject", {}),
        (f"/v1/admin/withdrawals/{withdrawal_id}/release", {"attestation": "x" * 20}),
    ):
        assert client.post(path, json=body, headers=bearer(readwrite_key)).status_code == 403


# -- outbound events -------------------------------------------------------


def test_events_are_emitted_with_the_ledger_changes(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    readwrite_key: str,
    admin_key: str,
) -> None:
    credit_user(session, user="noisy", amount=10_000_000)
    withdrawal_id = make_withdrawal(client, readwrite_key, user="noisy", amount=600_000)
    client.post(
        f"/v1/admin/withdrawals/{withdrawal_id}/approve", json={}, headers=bearer(admin_key)
    )
    submit(session_factory, fake_btcpay)
    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.broadcast_payout(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())
    fake_btcpay.complete_payout(payout_id)
    reconciliation.sweep_withdrawals(session_factory, fake_btcpay, get_settings())

    session.rollback()
    types = [
        row.event_type
        for row in session.execute(select(OutboundEvent).order_by(OutboundEvent.id)).scalars()
    ]
    assert event_service.WITHDRAWAL_PENDING_APPROVAL in types
    assert event_service.WITHDRAWAL_BROADCAST in types
    assert event_service.WITHDRAWAL_COMPLETED in types
