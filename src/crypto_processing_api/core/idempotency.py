"""`Idempotency-Key` handling for mutating endpoints.

The contract the platform sees:

- first use of a key      -> the operation runs
- same key, same body     -> the stored response is replayed, one ledger effect
- same key, different body-> 422, zero ledger effect (that is a client bug)
- duplicate still running -> 409 with Retry-After
- duplicate running, but the row has been `in_progress` for longer than the
  staleness window -> exactly one retry reclaims it and re-runs the handler

The staleness takeover exists because a crash between "row inserted" and "work
finished" would otherwise return 409 for the whole 72h TTL, with no way for the
caller to learn the id of the resource it already owns. `resource_id` is
recorded at the first commit precisely so a takeover can return it.

Expected call order in a handler:

    start = idempotency.begin(session, ...)      # nothing committed yet
    if isinstance(start, ReplayedResponse): return start
    ... create the business row ...
    idempotency.record_resource(session, ..., resource_id=str(row.id))
    session.commit()                             # row + idempotency record together
    ... external calls, never inside a transaction ...
    idempotency.complete(session, ..., status=201, body=payload)
    session.commit()
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from crypto_processing_api.ledger.models import IdempotencyKey

STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"

DEFAULT_STALE_AFTER_SECONDS = 60
RETRY_AFTER_SECONDS = 2
MAX_KEY_LENGTH = 255


class IdempotencyError(Exception):
    """Base class for the two failures a client can cause."""


class InvalidIdempotencyKey(IdempotencyError):
    """Missing or unusably long key."""


class RequestMismatch(IdempotencyError):
    """Same key, different body — 422. The client reused a key it must not have."""


class RequestInFlight(IdempotencyError):
    """A duplicate is running right now — 409 plus Retry-After."""

    def __init__(self, retry_after: int = RETRY_AFTER_SECONDS) -> None:
        super().__init__("a request with this Idempotency-Key is still in progress")
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class IdempotentStart:
    """The caller owns this key and must run the operation."""

    key: str
    endpoint: str
    request_hash: str
    reclaimed: bool
    resource_id: str | None


@dataclass(frozen=True, slots=True)
class ReplayedResponse:
    """A completed record: return this verbatim, run nothing."""

    status: int
    body: Any
    resource_id: str | None


def hash_request_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _validate_key(key: str) -> None:
    if not key or not key.strip():
        raise InvalidIdempotencyKey("Idempotency-Key must not be empty")
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidIdempotencyKey(f"Idempotency-Key exceeds {MAX_KEY_LENGTH} characters")


def begin(
    session: Session,
    *,
    key: str,
    endpoint: str,
    body: bytes,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> IdempotentStart | ReplayedResponse:
    """Claim `(key, endpoint)`, replay a completed response, or refuse.

    Does not commit. The caller commits the claim together with the business
    row it creates, so a durable idempotency record always implies a durable
    resource.
    """
    _validate_key(key)
    request_hash = hash_request_body(body)

    # A concurrent insert of the same key blocks here until the other
    # transaction ends, then reports zero rows. If that transaction rolled
    # back, the row is gone and the claim is ours on the next pass.
    for _ in range(3):
        claimed = session.execute(
            pg_insert(IdempotencyKey)
            .values(
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                state=STATE_IN_PROGRESS,
            )
            .on_conflict_do_nothing(index_elements=["key", "endpoint"])
            .returning(IdempotencyKey.key)
        ).scalar_one_or_none()
        if claimed is not None:
            return IdempotentStart(
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                reclaimed=False,
                resource_id=None,
            )

        existing = session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.key == key, IdempotencyKey.endpoint == endpoint
            )
        ).scalar_one_or_none()
        if existing is None:
            continue

        if existing.request_hash != request_hash:
            raise RequestMismatch(
                f"Idempotency-Key {key!r} was already used on {endpoint} with a different body"
            )
        if existing.state == STATE_COMPLETED:
            return ReplayedResponse(
                status=existing.response_status or 200,
                body=existing.response_body,
                resource_id=existing.resource_id,
            )

        # Stale takeover, as a single-winner CAS: whoever moves updated_at
        # forward owns the retry, everyone else gets 409.
        reclaim = cast(
            "CursorResult[Any]",
            session.execute(
                update(IdempotencyKey)
                .where(
                    IdempotencyKey.key == key,
                    IdempotencyKey.endpoint == endpoint,
                    IdempotencyKey.state == STATE_IN_PROGRESS,
                    IdempotencyKey.updated_at < func.now() - timedelta(seconds=stale_after_seconds),
                )
                .values(updated_at=func.now())
            ),
        )
        if reclaim.rowcount == 1:
            return IdempotentStart(
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                reclaimed=True,
                resource_id=existing.resource_id,
            )
        raise RequestInFlight()

    raise RequestInFlight()


def record_resource(session: Session, *, key: str, endpoint: str, resource_id: str) -> None:
    """Attach the created resource id, so a later takeover can return it."""
    session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key, IdempotencyKey.endpoint == endpoint)
        .values(resource_id=resource_id, updated_at=func.now())
    )


def complete(
    session: Session,
    *,
    key: str,
    endpoint: str,
    status: int,
    body: Any,
    resource_id: str | None = None,
) -> None:
    values: dict[str, Any] = {
        "state": STATE_COMPLETED,
        "response_status": status,
        "response_body": body,
        "updated_at": func.now(),
    }
    if resource_id is not None:
        values["resource_id"] = resource_id
    session.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key, IdempotencyKey.endpoint == endpoint)
        .values(**values)
    )


def purge_expired(session: Session, *, older_than_hours: int) -> int:
    """Drop records past their TTL. Returns how many rows went."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            delete(IdempotencyKey).where(
                IdempotencyKey.created_at < func.now() - timedelta(hours=older_than_hours)
            )
        ),
    )
    return result.rowcount or 0
