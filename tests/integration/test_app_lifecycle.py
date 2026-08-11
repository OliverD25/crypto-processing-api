"""Application startup and shutdown, and the session plumbing under it.

The rest of the suite deliberately skips the lifespan — the `client` fixture
says so, because running it would dispose the engine the other fixtures are
still using. So nothing exercised what happens when the process actually
starts, which is where the two decisions that matter live:

- **a missing payment method for a required asset raises**, and the process
  does not start. Invoices for a method the store does not serve produce
  deposit addresses nobody watches.
- **BTCPay being unreachable only logs.** That is an outage, not a
  misconfiguration, and balances and deposit reads are still serviceable.

`session_scope` is the other one. Every worker job runs inside it, so "rolls
back on any exception" is what keeps a half-written credit out of the ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from crypto_processing_api import db as db_module
from crypto_processing_api import main as main_module
from crypto_processing_api.config import get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.ledger.models import Asset, WorkerHeartbeat
from crypto_processing_api.main import create_app, sync_payment_methods_at_startup
from crypto_processing_api.services.assets import PaymentMethodMissing
from tests.fakes import FakeBTCPay


@pytest.fixture(autouse=True)
def restore_global_engine() -> None:
    """These tests dispose the process-wide engine on purpose.

    Nothing else depends on it — the fixtures build their own — and the next
    caller rebuilds it from the same settings.
    """
    yield
    db_module.dispose_engine()


def scratch_heartbeat(session: Session, name: str) -> WorkerHeartbeat | None:
    session.rollback()
    return session.execute(
        select(WorkerHeartbeat).where(WorkerHeartbeat.job_name == name)
    ).scalar_one_or_none()


# -- payment-method sync at startup ---------------------------------------


def test_an_unconfigured_btcpay_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deposit endpoints will fail, but balances and reads still work, so
    refusing to start would take a working half of the API down with it."""
    settings = get_settings()
    monkeypatch.setattr(settings, "btcpay_url", None)

    def refuse() -> None:
        raise AssertionError("no gateway may be built without BTCPay configured")

    monkeypatch.setattr(main_module, "get_gateway", refuse)
    assert sync_payment_methods_at_startup(settings) is None


def test_an_unreachable_btcpay_does_not_stop_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unreachable(FakeBTCPay):
        def get_store_payment_methods(self, *, only_enabled: bool = True) -> list:  # type: ignore[type-arg]
            raise BTCPayUnavailable("BTCPay unavailable on /payment-methods", status_code=503)

    monkeypatch.setattr(main_module, "get_gateway", Unreachable)
    assert sync_payment_methods_at_startup(get_settings()) is None


def test_a_reachable_btcpay_writes_the_resolved_ids(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = session.get(Asset, "BTC")
    assert asset is not None
    asset.btcpay_payment_method = "BTC-STALE"
    session.commit()

    monkeypatch.setattr(
        main_module, "get_gateway", lambda: FakeBTCPay(payment_methods=["BTC-CHAIN"])
    )
    sync_payment_methods_at_startup(get_settings())

    session.rollback()
    session.expire_all()
    assert session.get(Asset, "BTC").btcpay_payment_method == "BTC-CHAIN"  # type: ignore[union-attr]


def test_a_store_that_cannot_take_btc_stops_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """BTC is the asset this service exists for. A store that cannot serve it is
    a broken deployment, not a degraded one — so this one is allowed to raise."""
    monkeypatch.setattr(
        main_module, "get_gateway", lambda: FakeBTCPay(payment_methods=["LTC-CHAIN"])
    )
    with pytest.raises(PaymentMethodMissing, match="BTC"):
        sync_payment_methods_at_startup(get_settings())


# -- the lifespan ----------------------------------------------------------


def test_the_lifespan_runs_the_sync_and_then_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeBTCPay(payment_methods=["BTC-CHAIN"])
    monkeypatch.setattr(main_module, "get_gateway", lambda: fake)

    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert "get_store_payment_methods" in fake.calls


def test_shutdown_releases_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container is stopped and restarted on every deploy; a leaked pool
    holds Postgres connections that a 4GB box does not have spare."""
    monkeypatch.setattr(
        main_module, "get_gateway", lambda: FakeBTCPay(payment_methods=["BTC-CHAIN"])
    )

    with TestClient(create_app()) as client:
        client.get("/healthz")
        assert db_module._engine is not None

    assert db_module._engine is None
    assert db_module._session_factory is None


def test_create_app_accepts_explicit_settings() -> None:
    """The factory is used with an injected Settings by the contract exporter."""
    settings = get_settings()
    app = create_app(settings)
    assert app.state.settings is settings
    assert app.title == "crypto-processing-api"


def test_every_router_is_mounted() -> None:
    paths = set(create_app().openapi()["paths"])
    assert {
        "/healthz",
        "/readyz",
        "/v1/deposits",
        "/v1/withdrawals",
        "/v1/admin/wallet-alerts",
        "/webhooks/btcpay",
    } <= paths


# -- session plumbing ------------------------------------------------------


def test_the_engine_is_built_once_and_reused() -> None:
    first = db_module.get_engine()
    assert db_module.get_engine() is first
    assert db_module.get_session_factory() is db_module.get_session_factory()


def test_disposing_forces_a_fresh_engine() -> None:
    first = db_module.get_engine()
    db_module.dispose_engine()
    assert db_module._engine is None
    assert db_module.get_engine() is not first


def test_disposing_twice_is_harmless() -> None:
    """Shutdown can run after a failed startup that never built one."""
    db_module.dispose_engine()
    db_module.dispose_engine()
    assert db_module._engine is None


def test_session_scope_commits_on_success(session: Session) -> None:
    with db_module.session_scope() as scoped:
        scoped.add(
            WorkerHeartbeat(
                job_name="scope_commit", last_run_at=datetime.now(UTC), last_result="ok"
            )
        )

    row = scratch_heartbeat(session, "scope_commit")
    assert row is not None
    assert row.last_result == "ok"
    session.execute(text("DELETE FROM worker_heartbeats"))
    session.commit()


def test_session_scope_rolls_back_on_any_exception(session: Session) -> None:
    """A worker job that raises halfway must leave nothing behind."""
    with pytest.raises(RuntimeError, match="halfway"), db_module.session_scope() as scoped:
        scoped.add(WorkerHeartbeat(job_name="scope_rollback", last_run_at=datetime.now(UTC)))
        scoped.flush()
        raise RuntimeError("halfway through the job")

    assert scratch_heartbeat(session, "scope_rollback") is None


def test_the_request_dependency_closes_its_session() -> None:
    """FastAPI owns the commit; this only has to give the connection back."""
    generator = db_module.db_session()
    scoped = next(generator)
    scoped.execute(text("SELECT 1"))
    with pytest.raises(StopIteration):
        next(generator)
    assert not scoped.is_active or scoped.get_transaction() is None
