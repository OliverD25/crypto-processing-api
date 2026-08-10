"""Outbound platform events.

Rows are inserted in the **same transaction** as the ledger entry or state
change they announce. If emission were a separate transaction, a crash between
the two would either announce a balance the platform can never see, or move
money the platform is never told about — and the platform's reconciliation
would be chasing a ghost either way.

Delivery (signing, backoff, dead-letter) is M5. Until then rows accumulate as
`pending`, which is the correct resting state: the events happened.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from crypto_processing_api.core.ids import uuid7
from crypto_processing_api.ledger.models import OutboundEvent

DEPOSIT_DETECTED = "deposit.detected"
DEPOSIT_SETTLED = "deposit.settled"
DEPOSIT_REVIEW = "deposit.review_required"
DEPOSIT_EXPIRED = "deposit.expired"


def emit(session: Session, *, event_type: str, payload: dict[str, Any]) -> OutboundEvent:
    """Queue one event. Never commits — the caller's transaction owns it."""
    event = OutboundEvent(id=uuid7(), event_type=event_type, payload=payload)
    session.add(event)
    return event
