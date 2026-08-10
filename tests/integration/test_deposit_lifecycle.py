"""Deposit crediting end to end: webhooks, the poller, and the race between them."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.ledger import service as ledger
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Deposit,
    DepositPayment,
    DepositStatus,
    JournalEntry,
    OutboundEvent,
    WebhookEvent,
)
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.services import events as event_service
from crypto_processing_api.workers import webhook_processor
from tests.fakes import FakeBTCPay
from tests.integration.conftest import BTC, bearer, post_webhook

HALF_BTC = "0.50000000"
HALF_BTC_SATS = 50_000_000


def create_deposit(client: TestClient, key: str, *, user: str = "u1", idem: str = "d-1") -> Any:
    response = client.post(
        "/v1/deposits",
        json={"external_user_id": user, "asset": BTC},
        headers={**bearer(key), "Idempotency-Key": idem},
    )
    assert response.status_code == 201, response.text
    return response.json()


def available_balance(session: Session, user: str) -> int:
    """Read-only on purpose.

    get_user_accounts would INSERT the account rows and leave them uncommitted
    in this session, which then blocks the worker's own ON CONFLICT insert for
    the same user until the test's transaction ends. That is a deadlock between
    the test and the code under test, and it is entirely the test's fault.
    """
    session.rollback()
    account = session.execute(
        select(Account).where(
            Account.asset_id == BTC,
            Account.kind == AccountKind.USER_AVAILABLE,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


def drain(
    session_factory: sessionmaker[Session], fake: FakeBTCPay
) -> webhook_processor.ProcessReport:
    return webhook_processor.process_pending(session_factory, fake)


def test_happy_path_credits_the_exact_amount(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    deposit = create_deposit(client, readwrite_key, user="alice")
    invoice_id = next(iter(fake_btcpay.invoices))

    # Payment seen but unconfirmed.
    payment_id = fake_btcpay.add_payment(invoice_id, HALF_BTC, status="Processing")
    fake_btcpay.set_processing(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceProcessing", invoice_id))
    drain(session_factory, fake_btcpay)

    body = client.get(f"/v1/deposits/{deposit['deposit_id']}", headers=bearer(readwrite_key)).json()
    assert body["status"] == "confirming"
    assert body["payments"][0]["credited"] is False
    assert available_balance(session, "alice") == 0

    # Confirmed.
    fake_btcpay.settle(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    report = drain(session_factory, fake_btcpay)
    assert report.processed == 1

    body = client.get(f"/v1/deposits/{deposit['deposit_id']}", headers=bearer(readwrite_key)).json()
    assert body["status"] == "settled"
    assert body["amount_credited"] == HALF_BTC
    assert body["payments"][0]["payment_id"] == payment_id
    assert body["payments"][0]["credited"] is True
    assert available_balance(session, "alice") == HALF_BTC_SATS

    hot = ledger.get_system_account(session, asset_id=BTC, kind=AccountKind.HOT_WALLET)
    session.refresh(hot)
    assert hot.balance == HALF_BTC_SATS
    assert_ledger_consistent(session)


def test_source_ref_is_per_payment(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    create_deposit(client, readwrite_key)
    invoice_id = next(iter(fake_btcpay.invoices))
    payment_id = fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    drain(session_factory, fake_btcpay)

    entry = session.execute(select(JournalEntry)).scalar_one()
    assert entry.source_ref == f"btcpay_payment:{invoice_id}:{payment_id}"


def test_replaying_every_event_five_times_changes_nothing(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    create_deposit(client, readwrite_key, user="replay")
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)

    for event_type in (
        "InvoiceReceivedPayment",
        "InvoiceProcessing",
        "InvoicePaymentSettled",
        "InvoiceSettled",
    ):
        first = fake_btcpay.webhook_payload(event_type, invoice_id)
        post_webhook(client, fake_btcpay, first)
        for _ in range(4):
            post_webhook(
                client,
                fake_btcpay,
                fake_btcpay.webhook_payload(
                    event_type, invoice_id, original_delivery_id=first["deliveryId"]
                ),
            )
    drain(session_factory, fake_btcpay)
    # Drain again: reprocessing a processed queue must also be a no-op.
    drain(session_factory, fake_btcpay)

    assert available_balance(session, "replay") == HALF_BTC_SATS
    assert session.execute(select(func.count()).select_from(JournalEntry)).scalar_one() == 1
    assert session.execute(select(func.count()).select_from(DepositPayment)).scalar_one() == 1
    assert_ledger_consistent(session)


def test_poller_credits_when_webhooks_never_arrive(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    """The webhook-outage drill. Reconciliation is the correctness path."""
    deposit = create_deposit(client, readwrite_key, user="offline")
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)

    assert session.execute(select(func.count()).select_from(WebhookEvent)).scalar_one() == 0

    with session_factory() as poll_session:
        result = deposit_service.refresh_deposit(
            poll_session, fake_btcpay, deposit_id=uuid.UUID(deposit["deposit_id"])
        )
        poll_session.commit()

    assert result.status == DepositStatus.SETTLED
    assert result.credited_units == HALF_BTC_SATS
    assert available_balance(session, "offline") == HALF_BTC_SATS


def test_poller_racing_the_webhook_credits_once(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    """Real threads. Both triggers call the same transition function."""
    deposit = create_deposit(client, readwrite_key, user="racer")
    deposit_id = uuid.UUID(deposit["deposit_id"])
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)

    barrier = threading.Barrier(4)

    def apply(_index: int) -> int:
        with session_factory() as worker_session:
            barrier.wait(timeout=30)
            result = deposit_service.refresh_deposit(
                worker_session, fake_btcpay, deposit_id=deposit_id
            )
            worker_session.commit()
            return result.credited_units

    with ThreadPoolExecutor(max_workers=4) as pool:
        credited = [f.result(timeout=60) for f in [pool.submit(apply, i) for i in range(4)]]

    assert sum(credited) == HALF_BTC_SATS
    assert credited.count(HALF_BTC_SATS) == 1
    assert available_balance(session, "racer") == HALF_BTC_SATS
    assert session.execute(select(func.count()).select_from(JournalEntry)).scalar_one() == 1
    assert_ledger_consistent(session)


def test_multiple_payments_on_one_invoice_all_credit(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    create_deposit(client, readwrite_key, user="multi")
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, "0.10000000")
    fake_btcpay.add_payment(invoice_id, "0.25000000")
    fake_btcpay.settle(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    drain(session_factory, fake_btcpay)

    assert available_balance(session, "multi") == 35_000_000
    assert session.execute(select(func.count()).select_from(JournalEntry)).scalar_one() == 2


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda f, i: f.settle(i, additional_status="PaidLate"), "late payment"),
        (lambda f, i: f.settle(i, additional_status="Marked"), "a human marked it settled"),
        (lambda f, i: f.expire(i, additional_status="PaidLate"), "expired then paid"),
        (lambda f, i: f.invalidate(i), "BTCPay gave up on the invoice"),
    ],
)
def test_ambiguous_outcomes_go_to_review_uncredited(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
    mutate: Any,
    why: str,
) -> None:
    create_deposit(client, readwrite_key, user="review-user")
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    mutate(fake_btcpay, invoice_id)

    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    drain(session_factory, fake_btcpay)

    deposit = session.execute(select(Deposit)).scalar_one()
    assert deposit.status == DepositStatus.REVIEW, why
    assert available_balance(session, "review-user") == 0
    # Recorded, not dropped: the money reached custody and a human can see it.
    payment = session.execute(select(DepositPayment)).scalar_one()
    assert payment.credited_at is None
    assert payment.amount == HALF_BTC_SATS


def test_expired_with_nothing_received_is_just_expired(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    create_deposit(client, readwrite_key)
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.expire(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", invoice_id))
    drain(session_factory, fake_btcpay)

    assert session.execute(select(Deposit)).scalar_one().status == DepositStatus.EXPIRED


def test_admin_resolve_credits_through_the_normal_ledger_path(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
    admin_key: str,
) -> None:
    deposit = create_deposit(client, readwrite_key, user="late-payer")
    invoice_id = next(iter(fake_btcpay.invoices))
    payment_id = fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.expire(invoice_id, additional_status="PaidLate")
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", invoice_id))
    drain(session_factory, fake_btcpay)

    queue = client.get("/v1/admin/deposits/review", headers=bearer(admin_key)).json()
    assert [d["deposit_id"] for d in queue["deposits"]] == [deposit["deposit_id"]]

    response = client.post(
        f"/v1/admin/deposits/{deposit['deposit_id']}/resolve",
        json={"action": "credit", "payment_id": payment_id},
        headers=bearer(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["credited"] == HALF_BTC
    assert response.json()["deposit"]["status"] == "settled"
    assert available_balance(session, "late-payer") == HALF_BTC_SATS
    assert_ledger_consistent(session)

    credited = session.execute(select(DepositPayment)).scalar_one()
    assert credited.resolved_by is not None
    assert credited.resolved_by != "auto"


def test_resolve_takes_no_amount_from_the_operator(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
    admin_key: str,
) -> None:
    """An operator-supplied amount is not merely ignored; the field does not exist."""
    deposit = create_deposit(client, readwrite_key, user="fat-finger")
    invoice_id = next(iter(fake_btcpay.invoices))
    payment_id = fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.expire(invoice_id, additional_status="PaidLate")
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", invoice_id))
    drain(session_factory, fake_btcpay)

    response = client.post(
        f"/v1/admin/deposits/{deposit['deposit_id']}/resolve",
        json={"action": "credit", "payment_id": payment_id, "amount_units": "500000000"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 200
    # Ten times the real amount was offered and the server credited the truth.
    assert available_balance(session, "fat-finger") == HALF_BTC_SATS


def test_resolve_dismiss_credits_nothing(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
    admin_key: str,
) -> None:
    deposit = create_deposit(client, readwrite_key, user="dismissed")
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.invalidate(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceInvalid", invoice_id))
    drain(session_factory, fake_btcpay)

    response = client.post(
        f"/v1/admin/deposits/{deposit['deposit_id']}/resolve",
        json={"action": "dismiss"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 200
    assert session.execute(select(Deposit)).scalar_one().status == DepositStatus.DISMISSED
    assert available_balance(session, "dismissed") == 0


def test_resolve_requires_admin_scope(
    client: TestClient, readwrite_key: str, fake_btcpay: FakeBTCPay
) -> None:
    response = client.post(
        f"/v1/admin/deposits/{uuid.uuid4()}/resolve",
        json={"action": "dismiss"},
        headers=bearer(readwrite_key),
    )
    assert response.status_code == 403


def test_resolve_rejects_a_payment_btcpay_does_not_report(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    readwrite_key: str,
    admin_key: str,
) -> None:
    deposit = create_deposit(client, readwrite_key)
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.invalidate(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceInvalid", invoice_id))
    drain(session_factory, fake_btcpay)

    response = client.post(
        f"/v1/admin/deposits/{deposit['deposit_id']}/resolve",
        json={"action": "credit", "payment_id": "invented-payment"},
        headers=bearer(admin_key),
    )
    assert response.status_code == 404


def test_resolve_only_works_from_review(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    readwrite_key: str,
    admin_key: str,
) -> None:
    deposit = create_deposit(client, readwrite_key)
    invoice_id = next(iter(fake_btcpay.invoices))
    payment_id = fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    drain(session_factory, fake_btcpay)

    response = client.post(
        f"/v1/admin/deposits/{deposit['deposit_id']}/resolve",
        json={"action": "credit", "payment_id": payment_id},
        headers=bearer(admin_key),
    )
    assert response.status_code == 409


def test_orphan_event_is_flagged_not_credited(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
) -> None:
    """A cpapi invoice with no local row — what a restored backup looks like."""
    invoice = fake_btcpay.create_top_up_invoice(
        currency="BTC",
        metadata={"cpapi": True, "cpapi_version": 1, "deposit_id": str(uuid.uuid4())},
    )
    fake_btcpay.add_payment(invoice.id, HALF_BTC)
    fake_btcpay.settle(invoice.id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice.id))

    report = drain(session_factory, fake_btcpay)
    assert report.orphaned == 1
    assert session.execute(select(WebhookEvent)).scalar_one().status == "orphaned"
    assert session.execute(select(func.count()).select_from(JournalEntry)).scalar_one() == 0


def test_payout_events_are_ignored_until_m3(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
) -> None:
    post_webhook(
        client,
        fake_btcpay,
        {
            "deliveryId": "d-1",
            "originalDeliveryId": "d-1",
            "type": "PayoutUpdated",
            "storeId": fake_btcpay.store_id,
            "payoutId": "p-1",
        },
    )
    report = drain(session_factory, fake_btcpay)
    assert report.ignored == 1
    assert session.execute(select(WebhookEvent)).scalar_one().status == "ignored"


def test_a_failing_event_is_retried_then_parked(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    create_deposit(client, readwrite_key)
    invoice_id = next(iter(fake_btcpay.invoices))
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))

    from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable

    fake_btcpay.fail_next["get_invoice"] = BTCPayUnavailable("down")
    report = webhook_processor.process_pending(session_factory, fake_btcpay, max_attempts=2)
    assert report.retried == 1
    event = session.execute(select(WebhookEvent)).scalar_one()
    session.refresh(event)
    assert event.status == "received"
    assert event.attempts == 1

    fake_btcpay.fail_next["get_invoice"] = BTCPayUnavailable("still down")
    report = webhook_processor.process_pending(session_factory, fake_btcpay, max_attempts=2)
    assert report.failed == 1
    session.refresh(event)
    assert event.status == "failed"
    assert "BTCPayUnavailable" in (event.processing_error or "")


def test_outbound_events_are_emitted_with_the_ledger_entry(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    """Same transaction, or the platform hears about balances that do not exist."""
    create_deposit(client, readwrite_key, user="notified")
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    drain(session_factory, fake_btcpay)

    emitted = session.execute(select(OutboundEvent).order_by(OutboundEvent.id)).scalars().all()
    types = [e.event_type for e in emitted]
    assert event_service.DEPOSIT_DETECTED in types
    assert event_service.DEPOSIT_SETTLED in types
    # Delivery is M5; until then they wait.
    assert {e.status for e in emitted} == {"pending"}
    settled = next(e for e in emitted if e.event_type == event_service.DEPOSIT_SETTLED)
    assert settled.payload["amount_credited"] == HALF_BTC
    assert settled.payload["external_user_id"] == "notified"


def test_unparseable_payment_amount_goes_to_review(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
) -> None:
    """Never round into the ledger. A weird amount is a human's problem."""
    create_deposit(client, readwrite_key, user="weird")
    invoice_id = next(iter(fake_btcpay.invoices))
    fake_btcpay.add_payment(invoice_id, "0.000000001")
    fake_btcpay.settle(invoice_id)
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceSettled", invoice_id))
    drain(session_factory, fake_btcpay)

    assert session.execute(select(Deposit)).scalar_one().status == DepositStatus.REVIEW
    assert available_balance(session, "weird") == 0
    assert session.execute(select(func.count()).select_from(JournalEntry)).scalar_one() == 0


def test_a_resolved_late_payment_does_not_return_to_review(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
    admin_key: str,
) -> None:
    """Found on regtest: the sweep undid an admin's decision.

    The invoice stays Expired forever, so the status mapping keeps saying
    review. Without a check for uncredited payments the queue would refill with
    items a human had already cleared.
    """
    deposit = create_deposit(client, readwrite_key, user="resolved-late")
    invoice_id = next(iter(fake_btcpay.invoices))
    payment_id = fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.expire(invoice_id, additional_status="PaidLate")
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", invoice_id))
    drain(session_factory, fake_btcpay)

    client.post(
        f"/v1/admin/deposits/{deposit['deposit_id']}/resolve",
        json={"action": "credit", "payment_id": payment_id},
        headers=bearer(admin_key),
    )

    # Two more sweeps, exactly as the worker would run them.
    for _ in range(2):
        with session_factory() as poll_session:
            deposit_service.refresh_deposit(
                poll_session, fake_btcpay, deposit_id=uuid.UUID(deposit["deposit_id"])
            )
            poll_session.commit()

    body = client.get(f"/v1/deposits/{deposit['deposit_id']}", headers=bearer(readwrite_key)).json()
    assert body["status"] == "settled"
    assert body["amount_credited"] == HALF_BTC
    assert (
        client.get("/v1/admin/deposits/review", headers=bearer(admin_key)).json()["deposits"] == []
    )
    assert available_balance(session, "resolved-late") == HALF_BTC_SATS


def test_a_new_late_payment_after_a_resolve_reopens_review(
    client: TestClient,
    fake_btcpay: FakeBTCPay,
    session_factory: sessionmaker[Session],
    session: Session,
    readwrite_key: str,
    admin_key: str,
) -> None:
    """The other half: money that nobody has decided about still needs a human."""
    deposit = create_deposit(client, readwrite_key, user="twice-late")
    invoice_id = next(iter(fake_btcpay.invoices))
    first_payment = fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.expire(invoice_id, additional_status="PaidLate")
    post_webhook(client, fake_btcpay, fake_btcpay.webhook_payload("InvoiceExpired", invoice_id))
    drain(session_factory, fake_btcpay)
    client.post(
        f"/v1/admin/deposits/{deposit['deposit_id']}/resolve",
        json={"action": "credit", "payment_id": first_payment},
        headers=bearer(admin_key),
    )

    fake_btcpay.add_payment(invoice_id, "0.10000000")
    with session_factory() as poll_session:
        deposit_service.refresh_deposit(
            poll_session, fake_btcpay, deposit_id=uuid.UUID(deposit["deposit_id"])
        )
        poll_session.commit()

    body = client.get(f"/v1/deposits/{deposit['deposit_id']}", headers=bearer(readwrite_key)).json()
    assert body["status"] == "review"
    assert body["amount_credited"] == HALF_BTC
    assert available_balance(session, "twice-late") == HALF_BTC_SATS
