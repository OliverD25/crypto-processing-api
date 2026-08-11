"""What the webhook processor does with an event it cannot act on.

The endpoint only stores; this module decides what an event *means*. Its
classification is the interesting part, because three of the four answers look
identical from outside — the row is not credited either way — and they mean
completely different things:

- `ignored`  — not ours, or nothing to do. Normal, and silent.
- `orphaned` — ours by metadata, unknown locally. A human has to look.
- `failed`   — we tried and could not. Retried, then parked with its error.

Getting `orphaned` classified as `ignored` is the expensive mistake: it is the
restored-backup case, where BTCPay knows about a deposit and we do not, and no
webhook will ever mention it again.

Every event here is inserted the way the ingress endpoint inserts one, and the
processor is run through `process_pending`, so the claim query, the per-event
transaction and the attempt accounting are all real.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayNotFound, BTCPayUnavailable
from crypto_processing_api.ledger.models import (
    Deposit,
    DepositStatus,
    WebhookEvent,
    WithdrawalStatus,
)
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.services import withdrawals as withdrawal_service
from crypto_processing_api.workers import payout_submitter, webhook_processor
from crypto_processing_api.workers.webhook_processor import (
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_ORPHANED,
    STATUS_PROCESSED,
    STATUS_RECEIVED,
    ProcessReport,
)
from tests.fakes import FakeBTCPay, regtest_address
from tests.integration.conftest import BTC, credit_user

DEST = regtest_address("webhook-destination")
HALF_BTC = "0.50000000"
GROSS = 100_000
FUNDED = 1_000_000


def store_event(
    session: Session,
    *,
    event_type: str,
    invoice_id: str | None = None,
    payout_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    event = WebhookEvent(
        dedup_key=f"dedup-{uuid.uuid4()}",
        delivery_id=f"del-{uuid.uuid4()}",
        event_type=event_type,
        btcpay_invoice_id=invoice_id,
        btcpay_payout_id=payout_id,
        payload=payload if payload is not None else {},
        status=STATUS_RECEIVED,
    )
    session.add(event)
    session.commit()
    return event.id


def status_of(session: Session, event_id: int) -> str:
    session.rollback()
    event = session.get(WebhookEvent, event_id)
    assert event is not None
    return event.status


def drain(session_factory: sessionmaker[Session], fake: FakeBTCPay, **kwargs: Any) -> Any:
    return webhook_processor.process_pending(
        session_factory, fake, settings=get_settings(), **kwargs
    )


def make_deposit(session: Session, fake: FakeBTCPay, *, user: str) -> Deposit:
    deposit = deposit_service.create_deposit(session, external_user_id=user, asset_id=BTC)
    deposit_service.ensure_invoice(session, fake, get_settings(), deposit=deposit)
    session.commit()
    return deposit


# -- the report -----------------------------------------------------------


def test_the_report_totals_every_outcome() -> None:
    """`total` is what the worker logs as the job result, so a category left
    out of the sum would make a busy tick look idle."""
    report = ProcessReport(processed=1, ignored=2, orphaned=3, failed=4, retried=5)
    assert report.total == 15
    assert ProcessReport().total == 0


# -- events that mean nothing to us ---------------------------------------


@pytest.mark.parametrize(
    ("event_type", "why"),
    [
        ("PayoutCompleted", "a payout event this version does not act on"),
        ("InvoiceExpiredPaidPartial", "not one of the seven invoice events"),
        ("StoreCreated", "a store event that reached our endpoint at all"),
    ],
)
def test_an_event_we_do_not_act_on_is_ignored(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    event_type: str,
    why: str,
) -> None:
    event_id = store_event(session, event_type=event_type, invoice_id="inv-1")
    report = drain(session_factory, fake_btcpay)
    assert report.ignored == 1, why
    assert status_of(session, event_id) == STATUS_IGNORED


def test_an_invoice_event_with_no_invoice_id_is_ignored(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """There is nothing to re-fetch, so there is nothing this can decide."""
    event_id = store_event(session, event_type="InvoiceSettled", invoice_id=None)
    drain(session_factory, fake_btcpay)
    assert status_of(session, event_id) == STATUS_IGNORED


def test_an_invoice_that_is_not_ours_is_ignored(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """The store may serve other things. Someone else's invoice is not an
    incident, and paging an operator about one would train them to ignore the
    alert that matters."""
    event_id = store_event(
        session,
        event_type="InvoiceSettled",
        invoice_id="inv-someone-else",
        payload={"metadata": {"orderId": "shop-1234"}},
    )
    report = drain(session_factory, fake_btcpay)
    assert report.ignored == 1
    assert status_of(session, event_id) == STATUS_IGNORED


def test_our_invoice_with_no_local_row_is_orphaned_not_ignored(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """The restored-backup case: BTCPay knows about a deposit and we do not.
    No webhook will mention it again, so a human has to look."""
    event_id = store_event(
        session,
        event_type="InvoiceSettled",
        invoice_id="inv-restored",
        payload={"metadata": {"cpapi": True, "deposit_id": str(uuid.uuid4())}},
    )
    report = drain(session_factory, fake_btcpay)
    assert report.orphaned == 1
    assert status_of(session, event_id) == STATUS_ORPHANED


def test_a_deposit_is_found_by_metadata_when_the_row_has_no_invoice_id(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """The ambiguous-timeout shape again: the invoice exists, our row never
    learned its id. The deposit id BTCPay echoes back is what closes the gap."""
    deposit = make_deposit(session, fake_btcpay, user="metadata-found")
    invoice_id = deposit.btcpay_invoice_id
    assert invoice_id
    fake_btcpay.add_payment(invoice_id, HALF_BTC)
    fake_btcpay.settle(invoice_id)
    deposit.btcpay_invoice_id = None
    session.commit()

    event_id = store_event(
        session,
        event_type="InvoiceSettled",
        invoice_id=invoice_id,
        payload={"metadata": {"cpapi": True, "deposit_id": str(deposit.id)}},
    )
    report = drain(session_factory, fake_btcpay)

    assert report.processed == 1
    assert status_of(session, event_id) == STATUS_PROCESSED
    session.expire_all()
    assert session.get(Deposit, deposit.id).status is DepositStatus.SETTLED  # type: ignore[union-attr]


def test_metadata_without_a_deposit_id_finds_nothing(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    event_id = store_event(
        session,
        event_type="InvoiceSettled",
        invoice_id="inv-nameless",
        payload={"metadata": {"cpapi": True}},
    )
    drain(session_factory, fake_btcpay)
    assert status_of(session, event_id) == STATUS_ORPHANED


# -- payout events --------------------------------------------------------


def test_a_payout_event_with_no_payout_id_is_ignored(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    event_id = store_event(session, event_type="PayoutUpdated", payout_id=None)
    drain(session_factory, fake_btcpay)
    assert status_of(session, event_id) == STATUS_IGNORED


def test_someone_elses_payout_is_ignored(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    payout_id = fake_btcpay.create_foreign_payout(DEST, "0.001")
    event_id = store_event(session, event_type="PayoutUpdated", payout_id=payout_id)
    report = drain(session_factory, fake_btcpay)
    assert report.ignored == 1
    assert status_of(session, event_id) == STATUS_IGNORED


def test_our_payout_with_no_local_row_is_orphaned(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    payout = fake_btcpay.create_payout(
        destination=DEST,
        amount="0.001",
        payout_method_id="BTC-CHAIN",
        metadata={"cpapi": True, "withdrawal_id": str(uuid.uuid4())},
    )
    event_id = store_event(session, event_type="PayoutUpdated", payout_id=payout.id)
    report = drain(session_factory, fake_btcpay)
    assert report.orphaned == 1
    assert status_of(session, event_id) == STATUS_ORPHANED


def test_a_payout_whose_echoed_id_is_not_a_uuid_is_not_correlated(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """`uuid.UUID("not-a-uuid")` raises, and an uncaught ValueError here would
    park a perfectly ordinary event as failed and burn its attempt budget."""
    payout = fake_btcpay.create_payout(
        destination=DEST,
        amount="0.001",
        payout_method_id="BTC-CHAIN",
        metadata={"cpapi": True, "withdrawal_id": "not-a-uuid"},
    )
    event_id = store_event(session, event_type="PayoutUpdated", payout_id=payout.id)
    report = drain(session_factory, fake_btcpay)
    assert report.orphaned == 1
    assert status_of(session, event_id) == STATUS_ORPHANED


def test_a_payout_is_correlated_by_the_metadata_btcpay_echoes_back(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: FakeBTCPay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crashed-submission case: the payout exists, our row never learned
    its id. Without this correlation the withdrawal stays held forever."""
    credit_user(session, user="crashed", amount=FUNDED)
    outcome = withdrawal_service.place_hold(
        session,
        external_user_id="crashed",
        asset_id=BTC,
        amount_gross=GROSS,
        destination_address=DEST,
    )
    session.commit()
    withdrawal_id = outcome.withdrawal.id
    payout_submitter.submit_approved(session_factory, fake_btcpay, get_settings())
    session.expire_all()

    payout_id = withdrawal_service.get(session, withdrawal_id).backend_ref
    assert payout_id
    fake_btcpay.broadcast_payout(payout_id)
    # Forget the binding, exactly as a crash between the call and the write does.
    locked = withdrawal_service.lock(session, withdrawal_id)
    locked.backend_ref = None
    session.commit()

    event_id = store_event(session, event_type="PayoutUpdated", payout_id=payout_id)
    report = drain(session_factory, fake_btcpay)

    assert report.processed == 1
    assert status_of(session, event_id) == STATUS_PROCESSED
    session.expire_all()
    assert withdrawal_service.get(session, withdrawal_id).status is WithdrawalStatus.BROADCAST


# -- failure accounting ---------------------------------------------------


def test_an_invoice_btcpay_has_forgotten_is_parked_at_once(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """Terminal, not retried. Ten more attempts at an invoice BTCPay has
    dropped would just be ten more failures an hour apart."""
    deposit = make_deposit(session, fake_btcpay, user="pruned")
    invoice_id = deposit.btcpay_invoice_id
    assert invoice_id
    fake_btcpay.fail_next["get_invoice"] = BTCPayNotFound(f"no invoice {invoice_id}")

    event_id = store_event(session, event_type="InvoiceSettled", invoice_id=invoice_id)
    report = drain(session_factory, fake_btcpay)

    assert report.failed == 1
    assert report.retried == 0
    session.rollback()
    event = session.get(WebhookEvent, event_id)
    assert event is not None
    assert event.status == STATUS_FAILED
    assert event.attempts == 1
    assert "no longer has this invoice" in (event.processing_error or "")
    assert event.processed_at is not None


def test_a_transient_failure_is_retried_on_a_later_tick(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """The event stays `received` and is excluded from this pass, so a ten
    second BTCPay blip cannot burn the whole attempt budget in one go."""
    deposit = make_deposit(session, fake_btcpay, user="blip")
    invoice_id = deposit.btcpay_invoice_id
    assert invoice_id
    fake_btcpay.fail_next["get_invoice"] = BTCPayUnavailable("BTCPay is down", status_code=503)

    event_id = store_event(session, event_type="InvoiceSettled", invoice_id=invoice_id)
    report = drain(session_factory, fake_btcpay)

    assert report.retried == 1
    assert report.failed == 0
    session.rollback()
    event = session.get(WebhookEvent, event_id)
    assert event is not None
    assert event.status == STATUS_RECEIVED
    assert event.attempts == 1
    assert "BTCPayUnavailable" in (event.processing_error or "")


def test_the_attempt_budget_is_what_finally_parks_it(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    deposit = make_deposit(session, fake_btcpay, user="hopeless")
    invoice_id = deposit.btcpay_invoice_id
    assert invoice_id
    event_id = store_event(session, event_type="InvoiceSettled", invoice_id=invoice_id)

    for _ in range(3):
        fake_btcpay.fail_next["get_invoice"] = BTCPayUnavailable("down", status_code=503)
        drain(session_factory, fake_btcpay, max_attempts=3)

    session.rollback()
    event = session.get(WebhookEvent, event_id)
    assert event is not None
    assert event.status == STATUS_FAILED
    assert event.attempts == 3


def test_a_batch_stops_at_its_limit_and_leaves_the_rest(
    session: Session, session_factory: sessionmaker[Session], fake_btcpay: FakeBTCPay
) -> None:
    """One tick must not hold a transaction open over an unbounded queue."""
    for _ in range(4):
        store_event(session, event_type="StoreCreated")

    report = drain(session_factory, fake_btcpay, limit=2)

    assert report.total == 2
    session.rollback()
    remaining = session.execute(
        select(WebhookEvent).where(WebhookEvent.status == STATUS_RECEIVED)
    ).scalars()
    assert len(list(remaining)) == 2


def test_recording_a_failure_for_an_event_that_is_gone_is_a_no_op(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """The failure is recorded in a fresh transaction, so the row can have been
    removed in between. Raising here would lose the failure it came to record."""
    report = ProcessReport()
    webhook_processor._record_failure(
        session_factory, 999_999_999, 1, 10, "gone", report, terminal=True
    )
    assert report.failed == 0
    assert report.retried == 0
