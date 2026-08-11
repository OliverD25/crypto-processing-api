"""Outbound platform events.

Rows are inserted in the **same transaction** as the ledger entry or state
change they announce. If emission were a separate transaction, a crash between
the two would either announce a balance the platform can never see, or move
money the platform is never told about — and the platform's reconciliation
would be chasing a ghost either way.

Delivery (signing, backoff, dead-letter) is M5. Until then rows accumulate as
`pending`, which is the correct resting state: the events happened.

## Why the payloads are models

These payloads are a public contract — two SDKs and every integrator parse
them — and until now they were dicts built inline at four call sites, invisible
to the OpenAPI drift gate because they are not API responses. A field added to
one of those dicts reached every integrator with no CI signal anywhere.

So the payload of every event goes through a model here, and the emit sites
build the model rather than a dict. That makes drift impossible in the strict
sense: a field that is not on the model is dropped by `model_dump` and cannot
reach the wire at all. `scripts/export_openapi.py` writes the JSON Schema for
the eight envelopes to `docs/reference/webhook-events.json`, and CI fails if
the committed file no longer matches the code.

Amounts and timestamps are `str` for the same reason as in `api/schemas.py`:
they are already formatted by the time they get here, and letting Pydantic
re-serialize them would move bytes an integrator is parsing.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy.orm import Session

from crypto_processing_api.core.ids import uuid7
from crypto_processing_api.ledger.models import OutboundEvent

DEPOSIT_DETECTED = "deposit.detected"
DEPOSIT_SETTLED = "deposit.settled"
DEPOSIT_REVIEW = "deposit.review_required"
DEPOSIT_EXPIRED = "deposit.expired"

WITHDRAWAL_PENDING_APPROVAL = "withdrawal.pending_approval"
WITHDRAWAL_BROADCAST = "withdrawal.broadcast"
WITHDRAWAL_COMPLETED = "withdrawal.completed"
WITHDRAWAL_FAILED = "withdrawal.failed"

#: The closed set. A platform can switch exhaustively over it.
EVENT_TYPES = (
    DEPOSIT_DETECTED,
    DEPOSIT_SETTLED,
    DEPOSIT_REVIEW,
    DEPOSIT_EXPIRED,
    WITHDRAWAL_PENDING_APPROVAL,
    WITHDRAWAL_BROADCAST,
    WITHDRAWAL_COMPLETED,
    WITHDRAWAL_FAILED,
)


# -- payload bodies --------------------------------------------------------


class DepositEventData(BaseModel):
    deposit_id: str
    external_user_id: str
    asset: str
    status: str
    amount_credited: str = Field(
        description="Decimal string, the running total credited to the user for this deposit."
    )


class DepositDetectedData(DepositEventData):
    """A payment is visible. It is **not** credited yet — `amount_credited` may still be zero."""

    payments: list[str] = Field(description="BTCPay payment ids recorded by this update.")


class WithdrawalEventData(BaseModel):
    withdrawal_id: str
    external_user_id: str
    asset: str
    status: str
    amount_gross: str
    amount_net: str | None = Field(description="`null` until submission fixes the fee.")
    fee: str | None = Field(description="`null` until submission fixes the fee.")
    destination_address: str
    txid: str | None


class WithdrawalDecisionData(WithdrawalEventData):
    """A withdrawal that stopped, or stopped to wait. The reason is always present.

    Every path that queues a withdrawal names the gate that fired, and every
    path that fails one names why, so this field is required rather than
    optional — a `withdrawal.failed` with no reason would be the one event an
    operator cannot act on.
    """

    reason: str = Field(
        description="For `pending_approval`, which gate fired. For `failed`, why it "
        "will not proceed — including a machine-generated definitive-failure "
        "proof where the backend can produce one."
    )


# -- envelopes -------------------------------------------------------------


class _Envelope(BaseModel):
    """What is signed and POSTed: `{id, type, created_at, data}`.

    The signature covers these exact bytes, rendered with sorted keys and no
    whitespace — see `workers/outbound_delivery.event_body`. Verify against the
    raw request body, never against a re-serialization of it.
    """

    id: str = Field(
        description="Stable and `evt_`-prefixed. Dedup on it: the same id may arrive twice.",
        examples=["evt_019f3c1e-0a2b-7c4d-8e5f-6a7b8c9d0e1f"],
    )
    created_at: str = Field(examples=["2026-08-10T13:11:02+00:00"])


class DepositDetectedEvent(_Envelope):
    type: Literal["deposit.detected"]
    data: DepositDetectedData


class DepositSettledEvent(_Envelope):
    type: Literal["deposit.settled"]
    data: DepositEventData


class DepositReviewRequiredEvent(_Envelope):
    type: Literal["deposit.review_required"]
    data: DepositEventData


class DepositExpiredEvent(_Envelope):
    type: Literal["deposit.expired"]
    data: DepositEventData


class WithdrawalPendingApprovalEvent(_Envelope):
    type: Literal["withdrawal.pending_approval"]
    data: WithdrawalDecisionData


class WithdrawalBroadcastEvent(_Envelope):
    type: Literal["withdrawal.broadcast"]
    data: WithdrawalEventData


class WithdrawalCompletedEvent(_Envelope):
    type: Literal["withdrawal.completed"]
    data: WithdrawalEventData


class WithdrawalFailedEvent(_Envelope):
    """**The money is not back yet.** A failed withdrawal still holds the balance."""

    type: Literal["withdrawal.failed"]
    data: WithdrawalDecisionData


PlatformEvent = Annotated[
    DepositDetectedEvent
    | DepositSettledEvent
    | DepositReviewRequiredEvent
    | DepositExpiredEvent
    | WithdrawalPendingApprovalEvent
    | WithdrawalBroadcastEvent
    | WithdrawalCompletedEvent
    | WithdrawalFailedEvent,
    Field(discriminator="type"),
]

#: Parses and validates a delivered event. Also the source of the exported
#: JSON Schema, so the schema and the parser can never disagree.
PLATFORM_EVENT_ADAPTER: TypeAdapter[PlatformEvent] = TypeAdapter(PlatformEvent)


def event_schema() -> dict[str, Any]:
    """The JSON Schema for every outbound event, as one discriminated union."""
    schema = PLATFORM_EVENT_ADAPTER.json_schema(ref_template="#/$defs/{model}")
    schema["title"] = "crypto-processing-api outbound webhook events"
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def emit(session: Session, *, event_type: str, payload: dict[str, Any]) -> OutboundEvent:
    """Queue one event. Never commits — the caller's transaction owns it."""
    event = OutboundEvent(id=uuid7(), event_type=event_type, payload=payload)
    session.add(event)
    return event
