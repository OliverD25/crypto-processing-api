"""Delivers queued events to the platform.

The rows were written in the same transaction as the ledger change they
announce, so the queue never disagrees with the books. This worker is only
about getting them out.

Two things the design insists on, and the documentation repeats loudly:

- **these are notifications, not truth.** The platform must be able to re-read
  `GET /v1/deposits/{id}` and `GET /v1/users/{id}/balances`. Every integration
  that treats a webhook as authoritative eventually double-credits something.
- **a dead-lettered event is not deleted.** It is parked with its error, an
  operator is alerted, and it can be re-queued by hand.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crypto_processing_api.alerts.notifier import AlertCode, Severity, notify
from crypto_processing_api.config import Settings
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.core.signing import PLATFORM_SIGNATURE_HEADER, sign_platform_payload
from crypto_processing_api.ledger.models import OutboundEvent

logger = get_logger(__name__)

#: 1m, 5m, 30m, 2h, 6h, then every 12h to roughly three days. Ten attempts.
BACKOFF_SECONDS: tuple[int, ...] = (
    60,
    300,
    1_800,
    7_200,
    21_600,
    43_200,
    43_200,
    43_200,
    43_200,
    43_200,
)
MAX_ATTEMPTS = len(BACKOFF_SECONDS)

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_DEAD = "dead"


@dataclass
class DeliveryReport:
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    skipped: int = 0


def event_body(event: OutboundEvent) -> bytes:
    """The exact bytes that get signed and sent.

    Serialized once and reused for both, because signing one rendering and
    sending another is the classic way to ship a webhook nobody can verify.
    """
    document = {
        # `evt_` prefixed and stable, so the platform can dedup the way we
        # demand BTCPay's consumers do.
        "id": f"evt_{event.id}",
        "type": event.event_type,
        "created_at": event.created_at.isoformat(),
        "data": event.payload,
    }
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _next_attempt(attempts: int) -> datetime:
    delay = BACKOFF_SECONDS[min(attempts, MAX_ATTEMPTS - 1)]
    # Jitter so a platform coming back up is not hit by every parked event at
    # the same instant.
    jitter = delay * 0.2
    return datetime.now(UTC) + timedelta(seconds=delay + random.uniform(-jitter, jitter))  # noqa: S311


def _claim(session: Session, limit: int) -> list[OutboundEvent]:
    return list(
        session.execute(
            select(OutboundEvent)
            .where(
                OutboundEvent.status == STATUS_PENDING,
                OutboundEvent.next_attempt_at <= func.now(),
            )
            .order_by(OutboundEvent.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )


def deliver_pending(
    session_factory: Callable[[], Session],
    settings: Settings,
    *,
    limit: int = 20,
    client: httpx.Client | None = None,
) -> DeliveryReport:
    report = DeliveryReport()
    if not settings.outbound_configured:
        # A polling-only integration is a legitimate deployment. The events
        # stay pending rather than being dropped, so turning delivery on later
        # sends the backlog rather than losing it.
        return report

    owns_client = client is None
    http = client or httpx.Client(timeout=settings.outbound_http_timeout_seconds)
    try:
        with session_factory() as session:
            events = _claim(session, limit)
            for event in events:
                _deliver_one(session, http, settings, event, report)
            session.commit()
    finally:
        if owns_client:
            http.close()
    return report


def _deliver_one(
    session: Session,
    http: httpx.Client,
    settings: Settings,
    event: OutboundEvent,
    report: DeliveryReport,
) -> None:
    body = event_body(event)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        PLATFORM_SIGNATURE_HEADER: sign_platform_payload(
            settings.platform_webhook_secret or "", body, timestamp=timestamp
        ),
        "User-Agent": "crypto-processing-api",
    }

    attempts = event.attempts + 1
    try:
        response = http.post(str(settings.platform_webhook_url), content=body, headers=headers)
        # Any 2xx is delivered. The platform's opinion of the body is its own
        # business; our contract is that it received it.
        if 200 <= response.status_code < 300:
            event.status = STATUS_DELIVERED
            event.attempts = attempts
            event.last_error = None
            report.delivered += 1
            logger.info("outbound.delivered", event_id=str(event.id), type=event.event_type)
            return
        error = f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        error = type(exc).__name__

    event.attempts = attempts
    event.last_error = error[:500]
    if attempts >= MAX_ATTEMPTS:
        event.status = STATUS_DEAD
        report.dead += 1
        notify(
            Severity.WARNING,
            AlertCode.OUTBOUND_DEAD_LETTER,
            f"event {event.event_type} gave up after {attempts} attempts ({error}); "
            "it is parked, not lost — redeliver it once the platform is healthy",
            settings=settings,
            event_id=str(event.id),
        )
        return

    event.next_attempt_at = _next_attempt(attempts)
    report.retried += 1
    logger.warning("outbound.retry", event_id=str(event.id), attempts=attempts, error=error)


def requeue(session: Session, event_id: object) -> bool:
    """Put a dead event back in the queue. Attempts reset; the error is kept."""
    event = session.get(OutboundEvent, event_id)
    if event is None or event.status == STATUS_DELIVERED:
        return False
    event.status = STATUS_PENDING
    event.attempts = 0
    event.next_attempt_at = datetime.now(UTC)
    return True
