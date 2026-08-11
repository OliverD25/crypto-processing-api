"""Outbound delivery, Job C, alert routing and readiness."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.alerts import notifier
from crypto_processing_api.alerts.notifier import AlertCode, Severity
from crypto_processing_api.api.middleware import get_tron_gateway
from crypto_processing_api.config import get_settings
from crypto_processing_api.core.signing import (
    PLATFORM_SIGNATURE_HEADER,
    sign_platform_payload,
    verify_platform_signature,
)
from crypto_processing_api.ledger.models import (
    OutboundEvent,
    WorkerHeartbeat,
)
from crypto_processing_api.services import events as event_service
from crypto_processing_api.workers import outbound_delivery, reconciliation
from tests.fake_tron import FakeTronGrid
from tests.integration.conftest import BTC, bearer, credit_user

PLATFORM_URL = "https://platform.test/hooks/crypto"
SECRET = "platform-webhook-secret"


@pytest.fixture
def outbound_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_webhook_url", PLATFORM_URL)
    monkeypatch.setattr(settings, "platform_webhook_secret", SECRET)
    return settings


def queue_event(session: Session, event_type: str = "deposit.settled") -> OutboundEvent:
    event = event_service.emit(
        session, event_type=event_type, payload={"deposit_id": "d-1", "amount": "0.5"}
    )
    session.commit()
    return event


def transport_client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# -- signing ---------------------------------------------------------------


def test_the_signature_is_the_stripe_scheme() -> None:
    body = b'{"id":"evt_1"}'
    header = sign_platform_payload(SECRET, body, timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert verify_platform_signature(SECRET, body, header, now=1_700_000_000)


def test_a_changed_body_does_not_verify() -> None:
    header = sign_platform_payload(SECRET, b'{"id":"evt_1"}', timestamp=1_700_000_000)
    assert not verify_platform_signature(SECRET, b'{"id":"evt_2"}', header, now=1_700_000_000)


def test_an_old_signature_is_outside_the_replay_window() -> None:
    """The timestamp is inside the signed string precisely for this."""
    header = sign_platform_payload(SECRET, b"{}", timestamp=1_700_000_000)
    assert not verify_platform_signature(SECRET, b"{}", header, now=1_700_000_600)
    assert verify_platform_signature(SECRET, b"{}", header, now=1_700_000_100)


def test_a_forged_signature_does_not_verify() -> None:
    assert not verify_platform_signature(
        SECRET, b"{}", "t=1700000000,v1=" + "0" * 64, now=1_700_000_000
    )


# -- delivery --------------------------------------------------------------


def test_a_pending_event_is_delivered_and_signed(
    session: Session, session_factory: sessionmaker[Session], outbound_settings: Any
) -> None:
    queue_event(session)
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["signature"] = request.headers[PLATFORM_SIGNATURE_HEADER]
        return httpx.Response(200)

    report = outbound_delivery.deliver_pending(
        session_factory, outbound_settings, client=transport_client(handler)
    )
    assert report.delivered == 1

    document = json.loads(seen["body"])
    assert document["id"].startswith("evt_")
    assert document["type"] == "deposit.settled"
    assert document["data"]["deposit_id"] == "d-1"
    # Signed over the exact bytes sent, not a re-rendering of them.
    assert verify_platform_signature(SECRET, seen["body"], seen["signature"], now=int(time.time()))

    session.rollback()
    assert session.execute(select(OutboundEvent)).scalar_one().status == "delivered"


def test_a_failure_is_retried_with_backoff(
    session: Session, session_factory: sessionmaker[Session], outbound_settings: Any
) -> None:
    queue_event(session)
    report = outbound_delivery.deliver_pending(
        session_factory,
        outbound_settings,
        client=transport_client(lambda request: httpx.Response(500)),
    )
    assert report.retried == 1

    session.rollback()
    event = session.execute(select(OutboundEvent)).scalar_one()
    assert event.status == "pending"
    assert event.attempts == 1
    assert event.last_error == "HTTP 500"
    # Scheduled into the future, so the next tick does not hammer it.
    assert event.next_attempt_at > datetime.now(UTC)


def test_a_due_event_only_is_claimed(
    session: Session, session_factory: sessionmaker[Session], outbound_settings: Any
) -> None:
    queue_event(session)
    session.execute(text("UPDATE outbound_events SET next_attempt_at = now() + interval '1 hour'"))
    session.commit()

    report = outbound_delivery.deliver_pending(
        session_factory,
        outbound_settings,
        client=transport_client(lambda request: httpx.Response(200)),
    )
    assert report.delivered == 0


def test_exhausting_the_attempts_dead_letters_and_alerts(
    session: Session,
    session_factory: sessionmaker[Session],
    outbound_settings: Any,
    alerts: Any,
) -> None:
    queue_event(session)
    session.execute(
        text("UPDATE outbound_events SET attempts = :n"),
        {"n": outbound_delivery.MAX_ATTEMPTS - 1},
    )
    session.commit()

    report = outbound_delivery.deliver_pending(
        session_factory,
        outbound_settings,
        client=transport_client(lambda request: httpx.Response(503)),
    )
    assert report.dead == 1
    assert AlertCode.OUTBOUND_DEAD_LETTER.value in alerts.codes()

    session.rollback()
    event = session.execute(select(OutboundEvent)).scalar_one()
    assert event.status == "dead"
    # Parked, not deleted.
    assert event.payload["deposit_id"] == "d-1"


def test_without_a_platform_url_events_park_as_pending(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """A polling-only integration is legitimate; the backlog is kept."""
    queue_event(session)
    report = outbound_delivery.deliver_pending(session_factory, get_settings())
    assert report.delivered == 0

    session.rollback()
    assert session.execute(select(OutboundEvent)).scalar_one().status == "pending"


def test_an_admin_can_list_and_redeliver_a_dead_event(
    client: TestClient, session: Session, admin_key: str
) -> None:
    event = queue_event(session)
    session.execute(text("UPDATE outbound_events SET status = 'dead', attempts = 10"))
    session.commit()

    listed = client.get("/v1/admin/events?status=dead", headers=bearer(admin_key)).json()
    assert [row["raw_id"] for row in listed["events"]] == [str(event.id)]

    response = client.post(f"/v1/admin/events/{event.id}/redeliver", headers=bearer(admin_key))
    assert response.status_code == 200

    session.rollback()
    refreshed = session.execute(select(OutboundEvent)).scalar_one()
    assert refreshed.status == "pending"
    assert refreshed.attempts == 0


def test_redelivering_an_unknown_event_is_404(client: TestClient, admin_key: str) -> None:
    response = client.post(f"/v1/admin/events/{uuid.uuid4()}/redeliver", headers=bearer(admin_key))
    assert response.status_code == 404


def test_event_endpoints_need_admin_scope(client: TestClient, readwrite_key: str) -> None:
    assert client.get("/v1/admin/events", headers=bearer(readwrite_key)).status_code == 403


# -- Job C -----------------------------------------------------------------


def test_job_c_is_quiet_on_a_healthy_ledger(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    alerts: Any,
) -> None:
    credit_user(session, user="solvent", amount=1_000_000)
    fake_btcpay.wallet_balance = "0.01000000"
    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())
    assert report.consistent
    assert AlertCode.LEDGER_INVARIANT_FAILURE.value not in alerts.codes()


def test_job_c_alerts_on_any_drift(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    alerts: Any,
) -> None:
    """Drift is a severity-1 bug: the job alerts, it never repairs."""
    credit_user(session, user="drifted", amount=1_000_000)
    session.execute(text("UPDATE accounts SET balance = balance + 7 WHERE kind = 'hot_wallet'"))
    session.commit()

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())
    assert not report.consistent
    assert AlertCode.LEDGER_INVARIANT_FAILURE.value in alerts.codes()

    # Repair it so the autouse consistency fixture sees a clean database.
    session.execute(text("UPDATE accounts SET balance = balance - 7 WHERE kind = 'hot_wallet'"))
    session.commit()


def test_the_custody_tolerance_is_derived_from_in_flight_postings(
    session: Session,
    session_factory: sessionmaker[Session],
    fake_btcpay: Any,
    readwrite_key: str,
    client: TestClient,
) -> None:
    """Between submission and confirmation the coins have left the wallet.

    The ledger still carries them in payouts_in_flight, so the expected
    shortfall is exactly that balance — not a hand-tuned epsilon that would
    have to be loosened as volume grew.
    """
    credit_user(session, user="inflight", amount=1_000_000)
    response = client.post(
        "/v1/withdrawals",
        json={
            "external_user_id": "inflight",
            "asset": BTC,
            "amount": "100000",
            "destination_address": "bcrt1qakxe0syt7wkayqgccadyayhtfxhk9mvmvtq0sx",
        },
        headers={**bearer(readwrite_key), "Idempotency-Key": "w-inflight"},
    )
    assert response.status_code == 201
    from crypto_processing_api.workers import payout_submitter

    payout_submitter.submit_approved(session_factory, fake_btcpay, get_settings())

    report = reconciliation.check_invariants(session_factory, fake_btcpay, get_settings())
    line = next(row for row in report.custody if row.asset_id == BTC)
    assert line.ledger_in_flight > 0
    assert line.expected_shortfall == line.ledger_in_flight


def test_the_reconciliation_endpoint_reports_the_same_thing(
    client: TestClient,
    session: Session,
    admin_key: str,
    app: FastAPI,
    fake_btcpay: Any,
) -> None:
    app.dependency_overrides[get_tron_gateway] = FakeTronGrid
    credit_user(session, user="reported", amount=1_000_000)

    body = client.get("/v1/admin/reconciliation", headers=bearer(admin_key)).json()
    assert body["ledger_consistent"] is True
    btc = next(row for row in body["custody"] if row["asset"] == BTC)
    assert btc["user_obligations"] == "1000000"
    # Amounts are strings here too: a JS console must not truncate them.
    assert isinstance(btc["ledger_custody"], str)


# -- alerts ----------------------------------------------------------------


@pytest.mark.parametrize("code", list(AlertCode))
def test_every_alert_code_routes_through_the_transport(code: AlertCode, alerts: Any) -> None:
    notifier.notify(Severity.WARNING, code, f"test message for {code.value}")
    assert alerts.codes() == [code.value]


def test_an_alert_is_logged_even_with_no_transport() -> None:
    """A missing ntfy topic must not silence an alert."""
    notifier.set_transport(None)
    notifier.notify(Severity.CRITICAL, AlertCode.LEDGER_INVARIANT_FAILURE, "still logged")


def test_a_broken_transport_does_not_propagate(alerts: Any) -> None:
    class Broken:
        def send(self, severity: Severity, code: str, message: str) -> None:
            raise RuntimeError("notifier is down")

    notifier.set_transport(Broken())
    # A monitor that dies because its notifier is down stops watching.
    notifier.notify(Severity.WARNING, AlertCode.TRON_LOW_TRX_BALANCE, "low")


# -- readiness -------------------------------------------------------------


def test_healthz_stays_process_and_database_only(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert set(body) == {"status", "database", "version"}


def test_readyz_reports_components(client: TestClient, session: Session) -> None:
    session.execute(
        text(
            "INSERT INTO worker_heartbeats (job_name, last_run_at) VALUES ('webhooks', now()) "
            "ON CONFLICT (job_name) DO UPDATE SET last_run_at = now()"
        )
    )
    session.commit()

    response = client.get("/readyz")
    names = {row["name"] for row in response.json()["components"]}
    assert {"database", "btcpay", "worker"} <= names


def test_readyz_is_degraded_when_the_worker_stops(client: TestClient, session: Session) -> None:
    """Invisible from every other endpoint: reads keep answering 200."""
    stale = datetime.now(UTC) - timedelta(hours=2)
    session.execute(
        text(
            "INSERT INTO worker_heartbeats (job_name, last_run_at) VALUES ('webhooks', :t) "
            "ON CONFLICT (job_name) DO UPDATE SET last_run_at = :t"
        ),
        {"t": stale},
    )
    session.commit()

    response = client.get("/readyz")
    assert response.status_code == 503
    worker = next(row for row in response.json()["components"] if row["name"] == "worker")
    assert worker["status"] == "degraded"
    assert "last ran" in worker["detail"]

    session.execute(text("DELETE FROM worker_heartbeats"))
    session.commit()


def test_readyz_needs_no_api_key(client: TestClient) -> None:
    assert client.get("/readyz").status_code in (200, 503)


def test_heartbeats_are_recorded_by_the_runner(session: Session) -> None:
    from crypto_processing_api.workers.runner import record_heartbeat

    record_heartbeat("test_job", result="ok", error=None)
    session.rollback()
    row = session.execute(
        select(WorkerHeartbeat).where(WorkerHeartbeat.job_name == "test_job")
    ).scalar_one()
    assert row.last_result == "ok"

    record_heartbeat("test_job", result=None, error="boom")
    session.rollback()
    session.refresh(row)
    assert row.last_error == "boom"

    session.execute(text("DELETE FROM worker_heartbeats"))
    session.commit()
