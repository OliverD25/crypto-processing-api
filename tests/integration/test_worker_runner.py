"""The worker loop itself: the advisory lock, and job isolation.

Two properties keep a single-tenant deployment honest, and neither is visible
from any individual job:

- **a second worker container must do nothing.** Every job takes a Postgres
  advisory lock first. Started twice by accident, the duplicate finds the lock
  held and skips, so it cannot double-credit or double-poll.
- **one failing job must not kill the loop.** A BTCPay outage during the
  deposit sweep has to leave the withdrawal sweep, the outbound delivery and
  the invariant check running. A worker that dies on the first exception stops
  watching at exactly the moment something is wrong.

The lock is real Postgres, so the contention tests hold it on a second
connection rather than pretending.
"""

from __future__ import annotations

import signal
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.gateway.btcpay_client import BTCPayUnavailable
from crypto_processing_api.ledger.models import Asset, WorkerHeartbeat
from crypto_processing_api.workers import runner as runner_module
from crypto_processing_api.workers.runner import (
    Job,
    Runner,
    build_jobs,
    record_heartbeat,
    release_advisory_lock,
    run_locked,
    startup_sync,
    try_advisory_lock,
)
from tests.fake_tron import HOT_WALLET, FakeTronGrid
from tests.fakes import FakeBTCPay

TEST_LOCK_KEY = 9_001


def make_job(name: str, run: Callable[[], Any], *, lock_key: int = TEST_LOCK_KEY) -> Job:
    return Job(name=name, lock_key=lock_key, interval_seconds=60.0, run=run)


@pytest.fixture(autouse=True)
def clean_heartbeats(session: Session) -> Any:
    yield
    session.rollback()
    session.execute(text("DELETE FROM worker_heartbeats"))
    session.commit()


def heartbeat(session: Session, job_name: str) -> WorkerHeartbeat | None:
    session.rollback()
    return session.execute(
        select(WorkerHeartbeat).where(WorkerHeartbeat.job_name == job_name)
    ).scalar_one_or_none()


# -- the advisory lock -----------------------------------------------------


def test_a_job_runs_when_the_lock_is_free() -> None:
    ran: list[str] = []
    assert run_locked(make_job("solo", lambda: ran.append("yes") or "done")) == "done"
    assert ran == ["yes"]


def test_a_duplicate_worker_finds_the_lock_held_and_skips(session: Session) -> None:
    """This is the whole defence against a second replica being started."""
    assert try_advisory_lock(session, TEST_LOCK_KEY) is True
    try:
        ran: list[str] = []
        assert run_locked(make_job("contended", lambda: ran.append("yes"))) is None
        assert ran == []
    finally:
        release_advisory_lock(session, TEST_LOCK_KEY)
        session.commit()


def test_the_lock_is_given_back_after_the_job(session: Session) -> None:
    run_locked(make_job("first", lambda: "done"))
    # If the first run had leaked its lock, this would silently skip.
    assert try_advisory_lock(session, TEST_LOCK_KEY) is True
    release_advisory_lock(session, TEST_LOCK_KEY)
    session.commit()


def test_the_lock_is_given_back_even_when_the_job_raises(session: Session) -> None:
    """Otherwise one crash parks that job for the life of the process."""

    def boom() -> None:
        raise RuntimeError("job exploded")

    with pytest.raises(RuntimeError, match="job exploded"):
        run_locked(make_job("crasher", boom))

    assert try_advisory_lock(session, TEST_LOCK_KEY) is True
    release_advisory_lock(session, TEST_LOCK_KEY)
    session.commit()


def test_two_different_jobs_do_not_block_each_other(session: Session) -> None:
    assert try_advisory_lock(session, TEST_LOCK_KEY) is True
    try:
        ran: list[str] = []
        run_locked(make_job("other", lambda: ran.append("yes"), lock_key=TEST_LOCK_KEY + 1))
        assert ran == ["yes"]
    finally:
        release_advisory_lock(session, TEST_LOCK_KEY)
        session.commit()


# -- heartbeats ------------------------------------------------------------


def test_a_long_result_is_truncated_rather_than_rejected(session: Session) -> None:
    """A report's repr is not length-bounded, and a failed insert here would
    lose the only evidence the job ran at all."""
    record_heartbeat("verbose", result="x" * 5_000, error=None)
    row = heartbeat(session, "verbose")
    assert row is not None
    assert row.last_result is not None
    assert len(row.last_result) == 500


def test_an_empty_result_is_stored_as_nothing_not_as_an_empty_string(session: Session) -> None:
    record_heartbeat("blank", result="", error="")
    row = heartbeat(session, "blank")
    assert row is not None
    assert row.last_result is None
    assert row.last_error is None


# -- the loop --------------------------------------------------------------


def build_runner(jobs: list[Job]) -> Runner:
    runner = Runner(get_settings(), FakeBTCPay())
    runner.jobs = jobs
    return runner


def test_one_crashing_job_does_not_stop_the_ones_behind_it(session: Session) -> None:
    ran: list[str] = []

    def boom() -> None:
        raise ValueError("job exploded")

    runner = build_runner(
        [
            make_job("crasher", boom, lock_key=TEST_LOCK_KEY),
            make_job("survivor", lambda: ran.append("yes") or "ok", lock_key=TEST_LOCK_KEY + 1),
        ]
    )
    runner.jobs[-1].run = _stopping(runner, runner.jobs[-1].run)
    runner.run_forever(tick_seconds=0)

    assert ran == ["yes"]
    crashed = heartbeat(session, "crasher")
    assert crashed is not None
    # The type is in the message, because "boom" alone tells an operator nothing.
    assert crashed.last_error == "ValueError: job exploded"
    assert crashed.last_result is None
    survivor = heartbeat(session, "survivor")
    assert survivor is not None
    assert survivor.last_error is None


def test_a_btcpay_outage_is_recorded_without_taking_the_worker_down(session: Session) -> None:
    ran: list[str] = []

    def unavailable() -> None:
        raise BTCPayUnavailable("BTCPay unavailable on /api/v1/invoices", status_code=503)

    runner = build_runner(
        [
            make_job("deposit_sweep", unavailable, lock_key=TEST_LOCK_KEY),
            make_job(
                "withdrawal_sweep", lambda: ran.append("yes") or "ok", lock_key=TEST_LOCK_KEY + 1
            ),
        ]
    )
    runner.jobs[-1].run = _stopping(runner, runner.jobs[-1].run)
    runner.run_forever(tick_seconds=0)

    assert ran == ["yes"]
    row = heartbeat(session, "deposit_sweep")
    assert row is not None
    assert "BTCPay unavailable" in (row.last_error or "")


def test_a_job_that_reports_nothing_writes_no_heartbeat(session: Session) -> None:
    """`run_locked` returns None when the lock was held, and a skipped job must
    not refresh the heartbeat a stale-worker check reads."""
    runner = build_runner([make_job("quiet", lambda: None, lock_key=TEST_LOCK_KEY)])
    runner.jobs[0].run = _stopping(runner, runner.jobs[0].run)
    runner.run_forever(tick_seconds=0)
    assert heartbeat(session, "quiet") is None


def test_a_job_that_is_not_due_is_left_alone(session: Session) -> None:
    ran: list[str] = []
    early = make_job("early", lambda: ran.append("no") or "ok", lock_key=TEST_LOCK_KEY)
    # Far enough ahead that this tick cannot reach it.
    early.next_run_at = 1e12
    runner = build_runner([early, make_job("due", lambda: "ok", lock_key=TEST_LOCK_KEY + 1)])
    runner.jobs[-1].run = _stopping(runner, runner.jobs[-1].run)
    runner.run_forever(tick_seconds=0)

    assert ran == []
    assert heartbeat(session, "due") is not None


def test_a_stop_signal_is_honoured_before_the_next_job(session: Session) -> None:
    """SIGTERM during a deploy must not start one more job on the way out."""
    ran: list[str] = []
    runner = build_runner(
        [
            make_job("first", lambda: "ok", lock_key=TEST_LOCK_KEY),
            make_job("second", lambda: ran.append("no") or "ok", lock_key=TEST_LOCK_KEY + 1),
        ]
    )
    runner.jobs[0].run = _stopping(runner, runner.jobs[0].run)
    runner.run_forever(tick_seconds=0)

    assert ran == []
    assert runner.should_stop() is True
    assert heartbeat(session, "first") is not None
    assert heartbeat(session, "second") is None


def test_the_stop_handler_takes_a_signal_handler_signature() -> None:
    runner = build_runner([])
    assert runner.should_stop() is False
    runner.stop(signal.SIGTERM, None)
    assert runner.should_stop() is True


def _stopping(runner: Runner, inner: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap a job so the loop exits after it, without a sleep or a thread."""

    def run() -> Any:
        result = inner()
        runner.stop(signal.SIGTERM, None)
        return result

    return run


# -- the job list ----------------------------------------------------------


def test_a_btc_only_deployment_builds_no_tron_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with no TRON hot wallet must not call TronGrid at all."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", None)

    def refuse() -> None:
        raise AssertionError("get_tron_gateway must not be called without a hot wallet")

    monkeypatch.setattr(runner_module, "get_tron_gateway", refuse)

    names = [job.name for job in build_jobs(settings, FakeBTCPay())]
    assert "gas_monitor" not in names
    assert "manual_withdrawal_sweep" not in names
    # The BTC and bookkeeping jobs are all still there.
    assert {"webhooks", "deposit_sweep", "payout_submit", "invariants"} <= set(names)


def test_a_tron_deployment_adds_the_two_tron_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(runner_module, "get_tron_gateway", FakeTronGrid)

    names = [job.name for job in build_jobs(settings, FakeBTCPay())]
    assert "gas_monitor" in names
    assert "manual_withdrawal_sweep" in names


def test_the_invariant_job_is_given_the_tron_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without it `check_invariants` computed USDT custody with no chain source
    and short-circuited `insolvent` to False, so USDT insolvency was never once
    computed by the background job."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    fake_tron = FakeTronGrid()
    monkeypatch.setattr(runner_module, "get_tron_gateway", lambda: fake_tron)

    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        runner_module.reconciliation,
        "check_invariants",
        lambda factory, gateway, settings, tron=None: seen.update(tron=tron),
    )
    job = next(job for job in build_jobs(settings, FakeBTCPay()) if job.name == "invariants")
    job.run()
    assert seen["tron"] is fake_tron


def test_every_job_has_its_own_lock_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two jobs sharing a key would make each one block the other for no reason."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    monkeypatch.setattr(runner_module, "get_tron_gateway", FakeTronGrid)

    jobs = build_jobs(settings, FakeBTCPay())
    keys = [job.lock_key for job in jobs]
    assert len(set(keys)) == len(keys)


def test_every_job_has_a_positive_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "get_tron_gateway", FakeTronGrid)
    settings = get_settings()
    monkeypatch.setattr(settings, "tron_hot_wallet_address", HOT_WALLET)
    assert all(job.interval_seconds > 0 for job in build_jobs(settings, FakeBTCPay()))


# -- startup ---------------------------------------------------------------


def test_startup_sync_resolves_the_payment_methods_before_any_invoice(
    session: Session, engine: Engine
) -> None:
    """An invoice created for a stale payment-method id is a deposit address
    nobody is watching."""
    asset = session.get(Asset, "BTC")
    assert asset is not None
    asset.btcpay_payment_method = "BTC-STALE"
    session.commit()

    startup_sync(FakeBTCPay(payment_methods=["BTC-CHAIN"]))

    session.rollback()
    session.expire_all()
    assert session.get(Asset, "BTC").btcpay_payment_method == "BTC-CHAIN"  # type: ignore[union-attr]


def test_startup_sync_disables_an_asset_the_store_cannot_serve(session: Session) -> None:
    """USDT needs the USDt plugin. Refusing to boot without it would hold the
    BTC path hostage to an asset nobody configured."""
    startup_sync(FakeBTCPay(payment_methods=["BTC-CHAIN"]))

    session.rollback()
    session.expire_all()
    assert session.get(Asset, "USDT_TRC20").enabled is False  # type: ignore[union-attr]
    assert session.get(Asset, "BTC").enabled is True  # type: ignore[union-attr]


def test_main_wires_both_shutdown_signals_to_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A container gets SIGTERM on `docker stop` and SIGINT from a terminal.
    Missing either one turns a clean shutdown into a killed process."""
    fake = FakeBTCPay()
    monkeypatch.setattr(runner_module, "get_gateway", lambda: fake)

    synced: list[Any] = []
    monkeypatch.setattr(runner_module, "startup_sync", synced.append)

    installed: dict[int, Any] = {}
    monkeypatch.setattr(
        runner_module.signal, "signal", lambda sig, handler: installed.update({sig: handler})
    )

    started: list[Runner] = []
    monkeypatch.setattr(Runner, "run_forever", lambda self: started.append(self))

    assert runner_module.main() == 0
    assert synced == [fake]
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}
    assert started[0].gateway is fake
    for handler in installed.values():
        assert handler == started[0].stop


def test_the_settings_the_runner_holds_are_the_process_settings() -> None:
    runner = Runner(get_settings(), FakeBTCPay())
    assert runner.settings is get_settings()
    assert isinstance(runner.settings, Settings)
