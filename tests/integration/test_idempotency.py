"""Idempotency-Key semantics against a real database."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.core import idempotency
from crypto_processing_api.ledger.models import IdempotencyKey

ENDPOINT = "POST /v1/withdrawals"
BODY = json.dumps({"external_user_id": "u1", "asset": "BTC", "amount": "10000"}).encode()
OTHER_BODY = json.dumps({"external_user_id": "u1", "asset": "BTC", "amount": "99999"}).encode()


def begin(session: Session, key: str, body: bytes = BODY, **kwargs: Any) -> Any:
    return idempotency.begin(session, key=key, endpoint=ENDPOINT, body=body, **kwargs)


def age_row(session: Session, key: str, seconds: int) -> None:
    session.execute(
        text(
            "UPDATE idempotency_keys SET updated_at = now() - make_interval(secs => :secs) "
            "WHERE key = :key AND endpoint = :endpoint"
        ),
        {"secs": seconds, "key": key, "endpoint": ENDPOINT},
    )
    session.commit()


def test_first_use_claims_the_key(session: Session) -> None:
    start = begin(session, "idem-1")
    session.commit()
    assert isinstance(start, idempotency.IdempotentStart)
    assert start.reclaimed is False
    assert start.resource_id is None

    row = session.execute(select(IdempotencyKey).where(IdempotencyKey.key == "idem-1")).scalar_one()
    assert row.state == idempotency.STATE_IN_PROGRESS
    assert row.request_hash == idempotency.hash_request_body(BODY)


def test_completed_key_replays_the_stored_response(session: Session) -> None:
    begin(session, "idem-2")
    idempotency.complete(
        session,
        key="idem-2",
        endpoint=ENDPOINT,
        status=201,
        body={"withdrawal_id": "w-1", "status": "approved"},
        resource_id="w-1",
    )
    session.commit()

    replay = begin(session, "idem-2")
    assert isinstance(replay, idempotency.ReplayedResponse)
    assert replay.status == 201
    assert replay.body == {"withdrawal_id": "w-1", "status": "approved"}
    assert replay.resource_id == "w-1"


def test_same_key_different_body_is_a_client_bug(session: Session) -> None:
    begin(session, "idem-3")
    session.commit()
    with pytest.raises(idempotency.RequestMismatch):
        begin(session, "idem-3", body=OTHER_BODY)


def test_mismatch_is_detected_even_after_completion(session: Session) -> None:
    begin(session, "idem-4")
    idempotency.complete(session, key="idem-4", endpoint=ENDPOINT, status=201, body={"ok": True})
    session.commit()
    with pytest.raises(idempotency.RequestMismatch):
        begin(session, "idem-4", body=OTHER_BODY)


def test_fresh_in_progress_duplicate_gets_409(session: Session) -> None:
    begin(session, "idem-5")
    session.commit()
    with pytest.raises(idempotency.RequestInFlight) as excinfo:
        begin(session, "idem-5")
    assert excinfo.value.retry_after > 0


def test_stale_in_progress_row_is_reclaimable(session: Session) -> None:
    """Critique #10: a crash mid-operation must not mean 409 for the whole TTL."""
    begin(session, "idem-6")
    idempotency.record_resource(session, key="idem-6", endpoint=ENDPOINT, resource_id="w-42")
    session.commit()
    age_row(session, "idem-6", 120)

    retry = begin(session, "idem-6", stale_after_seconds=60)
    session.commit()
    assert isinstance(retry, idempotency.IdempotentStart)
    assert retry.reclaimed is True
    # The takeover learns which resource it already owns.
    assert retry.resource_id == "w-42"


def test_reclaim_resets_the_clock(session: Session) -> None:
    begin(session, "idem-7")
    session.commit()
    age_row(session, "idem-7", 120)

    assert isinstance(begin(session, "idem-7", stale_after_seconds=60), idempotency.IdempotentStart)
    session.commit()
    with pytest.raises(idempotency.RequestInFlight):
        begin(session, "idem-7", stale_after_seconds=60)


def test_key_scope_is_per_endpoint(session: Session) -> None:
    begin(session, "idem-8")
    session.commit()
    other = idempotency.begin(session, key="idem-8", endpoint="POST /v1/deposits", body=BODY)
    session.commit()
    assert isinstance(other, idempotency.IdempotentStart)


@pytest.mark.parametrize("key", ["", "   ", "k" * 256])
def test_unusable_keys_are_refused(session: Session, key: str) -> None:
    with pytest.raises(idempotency.InvalidIdempotencyKey):
        begin(session, key)


def test_purge_removes_only_expired_rows(session: Session) -> None:
    begin(session, "idem-old")
    session.commit()
    session.execute(text("UPDATE idempotency_keys SET created_at = now() - interval '100 hours'"))
    begin(session, "idem-new")
    session.commit()

    removed = idempotency.purge_expired(session, older_than_hours=72)
    session.commit()
    assert removed == 1
    remaining = session.execute(select(IdempotencyKey.key)).scalars().all()
    assert remaining == ["idem-new"]


def test_exactly_one_of_two_racing_reclaims_wins(
    session_factory: sessionmaker[Session],
) -> None:
    """The staleness takeover must not let two retries run the same operation."""
    with session_factory() as session:
        begin(session, "idem-race")
        session.commit()
        age_row(session, "idem-race", 300)

    barrier = threading.Barrier(4)

    def attempt(_index: int) -> str:
        with session_factory() as session:
            barrier.wait(timeout=30)
            try:
                begin(session, "idem-race", stale_after_seconds=60)
                session.commit()
            except idempotency.RequestInFlight:
                session.rollback()
                return "conflict"
            return "reclaimed"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = [
            future.result(timeout=60) for future in [pool.submit(attempt, i) for i in range(4)]
        ]

    assert outcomes.count("reclaimed") == 1


def test_concurrent_first_use_produces_one_owner(
    session_factory: sessionmaker[Session],
) -> None:
    barrier = threading.Barrier(4)

    def attempt(_index: int) -> str:
        with session_factory() as session:
            barrier.wait(timeout=30)
            try:
                outcome = begin(session, "idem-cold")
                session.commit()
            except idempotency.RequestInFlight:
                session.rollback()
                return "conflict"
            return "owner" if isinstance(outcome, idempotency.IdempotentStart) else "replay"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = [
            future.result(timeout=60) for future in [pool.submit(attempt, i) for i in range(4)]
        ]

    assert outcomes.count("owner") == 1
    assert outcomes.count("conflict") == 3
