"""Worker entrypoint.

    python -m crypto_processing_api.workers.runner

A plain loop, no Celery and no Redis: this is a single-tenant service on one
small box, and a scheduler with its own broker would be more moving parts than
the thing it schedules.

Every job takes a Postgres advisory lock before running. If a second worker
container is ever started by accident, it finds the lock held and skips, so a
duplicate replica cannot double-credit or double-poll.
"""

from __future__ import annotations

import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from crypto_processing_api.api.middleware import get_gateway
from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.core.redaction import configure_logging, get_logger
from crypto_processing_api.db import get_session_factory, session_scope
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway
from crypto_processing_api.services.assets import sync_payment_methods
from crypto_processing_api.workers import reconciliation, webhook_processor

logger = get_logger(__name__)

#: Namespace for pg_advisory_lock. Arbitrary, but stable: changing it would
#: make an old and a new worker stop seeing each other's locks.
LOCK_NAMESPACE = 0x6350

JOB_WEBHOOKS = 1
JOB_DEPOSIT_SWEEP = 2
JOB_ADOPT_CREATING = 3
JOB_ORPHAN_SCAN = 4
JOB_WALLET_SCAN = 5


@dataclass
class Job:
    name: str
    lock_key: int
    interval_seconds: float
    run: Callable[[], Any]
    next_run_at: float = field(default=0.0)


def try_advisory_lock(session: Session, key: int) -> bool:
    return bool(
        session.execute(
            text("SELECT pg_try_advisory_lock(:ns, :key)"), {"ns": LOCK_NAMESPACE, "key": key}
        ).scalar_one()
    )


def release_advisory_lock(session: Session, key: int) -> None:
    session.execute(
        text("SELECT pg_advisory_unlock(:ns, :key)"), {"ns": LOCK_NAMESPACE, "key": key}
    )


def run_locked(job: Job) -> Any:
    """Run one job while holding its lock, on a connection of its own."""
    factory = get_session_factory()
    session = factory()
    try:
        if not try_advisory_lock(session, job.lock_key):
            logger.info("job.skipped_locked", job=job.name)
            return None
        try:
            return job.run()
        finally:
            release_advisory_lock(session, job.lock_key)
            session.commit()
    finally:
        session.close()


def build_jobs(settings: Settings, gateway: BTCPayGateway) -> list[Job]:
    factory = get_session_factory()

    return [
        Job(
            name="webhooks",
            lock_key=JOB_WEBHOOKS,
            interval_seconds=settings.webhook_poll_interval_seconds,
            run=lambda: webhook_processor.process_pending(
                factory, gateway, max_attempts=settings.webhook_max_attempts
            ),
        ),
        Job(
            name="deposit_sweep",
            lock_key=JOB_DEPOSIT_SWEEP,
            interval_seconds=settings.reconcile_deposit_interval_seconds,
            run=lambda: reconciliation.sweep_deposits(factory, gateway, settings),
        ),
        Job(
            name="adopt_creating",
            lock_key=JOB_ADOPT_CREATING,
            interval_seconds=settings.reconcile_deposit_interval_seconds,
            run=lambda: reconciliation.adopt_stuck_creating(factory, gateway, settings),
        ),
        Job(
            name="orphan_scan",
            lock_key=JOB_ORPHAN_SCAN,
            interval_seconds=settings.reconcile_orphan_scan_interval_seconds,
            run=lambda: reconciliation.scan_for_orphan_invoices(factory, gateway, settings),
        ),
        Job(
            name="wallet_scan",
            lock_key=JOB_WALLET_SCAN,
            interval_seconds=settings.wallet_scan_interval_seconds,
            run=lambda: reconciliation.detect_unattributed_receives(factory, gateway, settings),
        ),
    ]


class Runner:
    def __init__(self, settings: Settings, gateway: BTCPayGateway) -> None:
        self.settings = settings
        self.gateway = gateway
        self.jobs = build_jobs(settings, gateway)
        self._stopping = False

    def stop(self, _signum: int, _frame: FrameType | None) -> None:
        logger.info("worker.stopping")
        self._stopping = True

    def should_stop(self) -> bool:
        """Read through a method on purpose.

        The flag is set from a signal handler, so it changes under the loop's
        feet. Reading the attribute directly lets a type checker conclude the
        inner check can never be true and call the shutdown path dead code.
        """
        return self._stopping

    def run_forever(self, *, tick_seconds: float = 1.0) -> None:
        logger.info("worker.started", jobs=[job.name for job in self.jobs])
        while not self.should_stop():
            now = time.monotonic()
            for job in self.jobs:
                if self.should_stop():
                    break
                if now < job.next_run_at:
                    continue
                job.next_run_at = now + job.interval_seconds
                try:
                    result = run_locked(job)
                    if result is not None:
                        logger.info("job.done", job=job.name, result=str(result))
                except BTCPayError as exc:
                    # A BTCPay outage must not take the worker down with it;
                    # the next tick tries again.
                    logger.warning("job.btcpay_error", job=job.name, error=str(exc))
                except Exception:
                    logger.exception("job.crashed", job=job.name)
            time.sleep(tick_seconds)
        logger.info("worker.stopped")


def startup_sync(gateway: BTCPayGateway) -> None:
    """Resolve payment method ids before any invoice is created with a stale one."""
    with session_scope() as session:
        report = sync_payment_methods(session, gateway)
    logger.info(
        "worker.payment_methods",
        resolved=report.resolved,
        updated=report.updated,
        disabled=report.disabled,
    )


def main() -> int:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    gateway = get_gateway()

    startup_sync(gateway)

    runner = Runner(settings, gateway)
    signal.signal(signal.SIGINT, runner.stop)
    signal.signal(signal.SIGTERM, runner.stop)
    runner.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
