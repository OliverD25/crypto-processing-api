"""Processes durable `webhook_events` rows.

The endpoint only stores; this decides what an event means. Each event is
handled in its own transaction holding `FOR UPDATE SKIP LOCKED` on its row, so
a poisonous event cannot roll back the ones around it and a second worker
cannot pick up one already in flight.

Nothing here trusts the stored payload for amounts. It carries ids and flags;
the handler re-fetches the invoice from Greenfield and lets
`apply_invoice_state` decide.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.gateway.btcpay_client import BTCPayGateway, BTCPayNotFound
from crypto_processing_api.ledger.models import Deposit, WebhookEvent, Withdrawal
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.services import withdrawals as withdrawal_service

logger = get_logger(__name__)

STATUS_RECEIVED = "received"
STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"
STATUS_IGNORED = "ignored"
STATUS_ORPHANED = "orphaned"

#: Events that move deposit state. Everything else is recorded and ignored.
PAYOUT_EVENTS = frozenset({"PayoutCreated", "PayoutApproved", "PayoutUpdated"})

INVOICE_EVENTS = frozenset(
    {
        "InvoiceCreated",
        "InvoiceReceivedPayment",
        "InvoiceProcessing",
        "InvoicePaymentSettled",
        "InvoiceSettled",
        "InvoiceExpired",
        "InvoiceInvalid",
    }
)


@dataclass
class ProcessReport:
    processed: int = 0
    ignored: int = 0
    orphaned: int = 0
    failed: int = 0
    retried: int = 0

    @property
    def total(self) -> int:
        return self.processed + self.ignored + self.orphaned + self.failed + self.retried


def _claim(session: Session, exclude: set[int]) -> WebhookEvent | None:
    query = select(WebhookEvent).where(WebhookEvent.status == STATUS_RECEIVED)
    if exclude:
        # An event that just failed stays `received` for a later tick. Without
        # this it would be re-claimed immediately and burn its whole attempt
        # budget in one pass, turning a ten-second BTCPay blip into a
        # permanently parked event.
        query = query.where(WebhookEvent.id.notin_(exclude))
    return session.execute(
        query.order_by(WebhookEvent.received_at).limit(1).with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def _find_deposit(session: Session, event: WebhookEvent) -> Deposit | None:
    if event.btcpay_invoice_id:
        found = session.execute(
            select(Deposit).where(Deposit.btcpay_invoice_id == event.btcpay_invoice_id)
        ).scalar_one_or_none()
        if found is not None:
            return found
    # An invoice created just before an ambiguous timeout has no invoice id on
    # the row yet, so fall back to the deposit id BTCPay echoes in metadata.
    metadata = event.payload.get("metadata") if isinstance(event.payload, dict) else None
    deposit_id = deposit_service.deposit_id_from_metadata(metadata)
    if deposit_id is None:
        return None
    return session.get(Deposit, deposit_id)


def handle_event(session: Session, gateway: BTCPayGateway, event: WebhookEvent) -> str:
    """Return the status this event should end in."""
    if event.event_type in PAYOUT_EVENTS:
        return _handle_payout_event(session, gateway, event)
    if event.event_type.startswith("Payout"):
        return STATUS_IGNORED
    if event.event_type not in INVOICE_EVENTS:
        return STATUS_IGNORED
    if not event.btcpay_invoice_id:
        return STATUS_IGNORED

    deposit = _find_deposit(session, event)
    if deposit is None:
        metadata = event.payload.get("metadata") if isinstance(event.payload, dict) else None
        if deposit_service.is_cpapi_invoice(metadata):
            # Ours by metadata, unknown locally — a restored backup, or a row
            # that never committed. A human has to look.
            logger.warning(
                "webhook.orphan",
                invoice=event.btcpay_invoice_id,
                event_type=event.event_type,
            )
            return STATUS_ORPHANED
        return STATUS_IGNORED

    invoice = gateway.get_invoice(event.btcpay_invoice_id)
    deposit_service.apply_invoice_state(session, gateway, deposit_id=deposit.id, invoice=invoice)
    return STATUS_PROCESSED


def _handle_payout_event(session: Session, gateway: BTCPayGateway, event: WebhookEvent) -> str:
    """Payout events carry ids and states, never amounts or transaction ids.

    So the handler always follows up with a GET: the txid lives in
    `paymentProof`, which the webhook payload does not include, and the state
    in the payload can already be stale by the time we read it.
    """
    payout_id = event.btcpay_payout_id
    if not payout_id:
        return STATUS_IGNORED

    withdrawal = session.execute(
        select(Withdrawal).where(Withdrawal.backend_ref == payout_id)
    ).scalar_one_or_none()

    payout = gateway.get_payout(payout_id)
    if withdrawal is None:
        # Correlate by the metadata BTCPay echoes back. This is the case a
        # crashed submission leaves behind: the payout exists, our row never
        # learned its id.
        echoed = payout.metadata.get("withdrawal_id")
        if echoed:
            try:
                withdrawal = session.get(Withdrawal, uuid.UUID(str(echoed)))
            except ValueError:
                withdrawal = None
        if withdrawal is None:
            if payout.metadata.get("cpapi"):
                logger.warning("webhook.payout_orphan", payout=payout_id)
                return STATUS_ORPHANED
            return STATUS_IGNORED

    withdrawal_service.apply_payout_state(session, withdrawal_id=withdrawal.id, payout=payout)
    return STATUS_PROCESSED


def process_pending(
    session_factory: Callable[[], Session],
    gateway: BTCPayGateway,
    *,
    limit: int = 25,
    max_attempts: int = 10,
) -> ProcessReport:
    report = ProcessReport()
    deferred: set[int] = set()

    for _ in range(limit):
        with session_factory() as session:
            event = _claim(session, deferred)
            if event is None:
                return report
            event_id = event.id
            attempts = event.attempts
            try:
                outcome = handle_event(session, gateway, event)
                event.status = outcome
                event.processed_at = datetime.now(UTC)
                event.attempts = attempts + 1
                session.commit()
            except BTCPayNotFound:
                session.rollback()
                _record_failure(
                    session_factory,
                    event_id,
                    attempts + 1,
                    max_attempts,
                    "BTCPay no longer has this invoice",
                    report,
                    terminal=True,
                )
                continue
            except Exception as exc:
                session.rollback()
                deferred.add(event_id)
                _record_failure(
                    session_factory,
                    event_id,
                    attempts + 1,
                    max_attempts,
                    f"{type(exc).__name__}: {exc}",
                    report,
                    terminal=False,
                )
                continue

            if outcome == STATUS_PROCESSED:
                report.processed += 1
            elif outcome == STATUS_ORPHANED:
                report.orphaned += 1
            else:
                report.ignored += 1

    return report


def _record_failure(
    session_factory: Callable[[], Session],
    event_id: int,
    attempts: int,
    max_attempts: int,
    error: str,
    report: ProcessReport,
    *,
    terminal: bool,
) -> None:
    """Bump the attempt counter in a transaction of its own.

    The failed handler's transaction is gone, so this needs a clean session, or
    the attempt count rolls back with the failure it is meant to record.
    """
    with session_factory() as session:
        event = session.get(WebhookEvent, event_id)
        if event is None:
            return
        event.attempts = attempts
        event.processing_error = error[:1000]
        if terminal or attempts >= max_attempts:
            event.status = STATUS_FAILED
            event.processed_at = datetime.now(UTC)
            report.failed += 1
            logger.error("webhook.gave_up", event_id=event_id, attempts=attempts, error=error)
        else:
            report.retried += 1
            logger.warning("webhook.retry", event_id=event_id, attempts=attempts, error=error)
        session.commit()
