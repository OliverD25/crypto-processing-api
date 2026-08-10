#!/usr/bin/env python
"""Produce a frozen database dump for the upgrade test suite.

    docker compose -f deploy/docker-compose.test.yml up -d
    python scripts/make_upgrade_fixture.py --version 0.1.1

Writes `tests/fixtures/upgrade/v<version>.sql`: a schema-and-data dump of a
database at the current migration head, seeded with a scenario that touches
every table the money path uses.

The point is the artifact, not the script. Each release leaves behind a dump
that is **never edited again**, and every future migration must be able to
upgrade it. That is what stops migration rot: not a test someone remembers to
update, but a growing pile of real old databases that a broken migration
cannot get past.

The alternative — reconstructing an old schema from a git tag inside CI —
was rejected. It needs the old code importable beside the new code, and it
breaks the first time a dependency pin bit-rots. A dump executes no old code
at all.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "upgrade"
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.test.yml"

SCRATCH_DB = "cpapi_fixture_build"
ADMIN_URL = "postgresql+psycopg://cpapi:cpapi@127.0.0.1:54329/cpapi_test"
SCRATCH_URL = f"postgresql+psycopg://cpapi:cpapi@127.0.0.1:54329/{SCRATCH_DB}"

#: Deterministic ids, so the dump is stable and the expectations in
#: tests/integration/test_migration_upgrade.py can name exact rows.
USER_A = "fixture-user-a"
USER_B = "fixture-user-b"
DEPOSIT_ID = uuid.UUID("019fe000-0000-7000-8000-00000000d001")
WITHDRAWAL_CONFIRMED = uuid.UUID("019fe000-0000-7000-8000-00000000w001".replace("w", "b"))
WITHDRAWAL_PENDING = uuid.UUID("019fe000-0000-7000-8000-00000000b002")
WITHDRAWAL_MANUAL = uuid.UUID("019fe000-0000-7000-8000-00000000b003")
FROZEN_AT = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def log(message: str) -> None:
    print(f"[fixture] {message}", file=sys.stderr)


def run(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        args, capture_output=True, text=True, check=False, cwd=REPO_ROOT
    )
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed:\n{result.stderr[:2000]}")
    return result.stdout


def recreate_scratch_database() -> None:
    # PostgreSQL cannot bind a database name as a parameter — an identifier is
    # not a value — so these two cannot be parameterized by any means. SCRATCH_DB
    # is a module constant that never sees input. Suppressed with a reason and
    # logged in SECURITY-AUDIT.md rather than silenced; see the triage table.
    engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        connection.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    engine.dispose()
    log(f"recreated {SCRATCH_DB}")


def migrate_scratch_database() -> None:
    from alembic import command

    from tests.integration.conftest import make_alembic_config

    command.upgrade(make_alembic_config(SCRATCH_URL), "head")
    log("migrated to head")


def seed(session: Session) -> None:
    """A scenario touching every table the money path writes.

    Deliberately includes a manual-TRON withdrawal and a webhook event: those
    are the rows a future migration is most likely to get wrong, because they
    are the ones nobody has in their head while writing it.
    """
    from crypto_processing_api.cli import asset_specs
    from crypto_processing_api.config import get_settings
    from crypto_processing_api.ledger import service as ledger
    from crypto_processing_api.ledger.models import (
        AccountKind,
        Deposit,
        DepositPayment,
        DepositStatus,
        EntryKind,
        OutboundEvent,
        WebhookEvent,
        WithdrawalStatus,
        WorkerHeartbeat,
    )
    from crypto_processing_api.services import withdrawals as withdrawal_service

    ledger.seed_assets(session, asset_specs(get_settings()))
    session.commit()

    # Two funded users on BTC, one on USDT.
    for user, amount in ((USER_A, 500_000_000), (USER_B, 120_000_000)):
        hot = ledger.get_system_account(session, asset_id="BTC", kind=AccountKind.HOT_WALLET)
        available, _hold = ledger.get_user_accounts(session, asset_id="BTC", external_user_id=user)
        ledger.post_entry(
            session,
            kind=EntryKind.DEPOSIT_CREDIT,
            asset_id="BTC",
            source_ref=f"btcpay_payment:fixture-{user}:0",
            postings=[(hot.id, amount), (available.id, -amount)],
        )
    usdt_hot = ledger.get_system_account(
        session, asset_id="USDT_TRC20", kind=AccountKind.HOT_WALLET
    )
    usdt_available, _ = ledger.get_user_accounts(
        session, asset_id="USDT_TRC20", external_user_id=USER_A
    )
    ledger.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id="USDT_TRC20",
        source_ref="btcpay_payment:fixture-usdt:0",
        postings=[(usdt_hot.id, 900_000_000), (usdt_available.id, -900_000_000)],
    )
    session.commit()

    # A settled deposit with its payment row.
    deposit = Deposit(
        id=DEPOSIT_ID,
        external_user_id=USER_A,
        asset_id="BTC",
        btcpay_invoice_id="fixture-invoice-1",
        amount_expected=500_000_000,
        amount_credited=500_000_000,
        status=DepositStatus.SETTLED,
        address="bcrt1qakxe0syt7wkayqgccadyayhtfxhk9mvmvtq0sx",
        checkout_link="https://btcpay.example/i/fixture-invoice-1",
        expires_at=FROZEN_AT + timedelta(hours=1),
        monitoring_expires_at=FROZEN_AT + timedelta(days=1),
        address_reserved_from=FROZEN_AT,
        address_reserved_until=FROZEN_AT + timedelta(days=1),
    )
    session.add(deposit)
    session.flush()
    session.add(
        DepositPayment(
            id=uuid.UUID("019fe000-0000-7000-8000-0000000000p1".replace("p", "a")),
            deposit_id=DEPOSIT_ID,
            btcpay_payment_id="a" * 64 + "-0",
            amount=500_000_000,
            credited_at=FROZEN_AT,
            resolved_by="auto",
        )
    )

    # A confirmed BTC withdrawal, walked through the real service so its
    # entries and balances are genuine rather than invented.
    confirmed = withdrawal_service.place_hold(
        session,
        external_user_id=USER_A,
        asset_id="BTC",
        amount_gross=10_000_000,
        destination_address="bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj",
    )
    session.commit()
    from crypto_processing_api.services.fees import FeeQuote

    quote = FeeQuote(
        fee=30_000, net=9_970_000, wallet_fee=30_000, sat_per_vb=10.0, source="fixture"
    )
    confirmed.withdrawal.fee_amount = quote.fee
    confirmed.withdrawal.amount_net = quote.net
    confirmed.withdrawal.backend_ref = "fixture-payout-1"
    confirmed.withdrawal.txid = "b" * 64
    confirmed.withdrawal.submitted_at = FROZEN_AT
    withdrawal_service.post_submit_entry(session, confirmed.withdrawal, quote)
    withdrawal_service.post_settle_entry(session, confirmed.withdrawal)
    confirmed.withdrawal.status = WithdrawalStatus.CONFIRMED
    session.commit()

    # A BTC withdrawal still waiting for an admin.
    pending = withdrawal_service.place_hold(
        session,
        external_user_id=USER_B,
        asset_id="BTC",
        amount_gross=60_000_000,
        destination_address="bcrt1q2fhpadugqsm3twzvpg8veawxeudcsaq7ufxxfj",
    )
    session.commit()

    # A manual-TRON withdrawal in the approval queue.
    manual = withdrawal_service.place_hold(
        session,
        external_user_id=USER_A,
        asset_id="USDT_TRC20",
        amount_gross=200_000_000,
        destination_address="TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
        backend=withdrawal_service.BACKEND_MANUAL_TRON,
        force_manual=True,
    )
    session.commit()

    session.add(
        WebhookEvent(
            dedup_key="fixture-delivery-1",
            delivery_id="fixture-delivery-1",
            event_type="InvoiceSettled",
            btcpay_invoice_id="fixture-invoice-1",
            payload={"type": "InvoiceSettled", "invoiceId": "fixture-invoice-1"},
            status="processed",
            processed_at=FROZEN_AT,
            attempts=1,
        )
    )
    session.add(
        OutboundEvent(
            id=uuid.UUID("019fe000-0000-7000-8000-0000000000e1".replace("e", "c")),
            event_type="deposit.settled",
            payload={"deposit_id": str(DEPOSIT_ID), "amount_credited": "5.00000000"},
            status="pending",
        )
    )
    session.add(WorkerHeartbeat(job_name="webhooks", last_run_at=FROZEN_AT, last_result="ok"))
    session.commit()

    log(
        "seeded: "
        f"withdrawals confirmed={confirmed.withdrawal.id} pending={pending.withdrawal.id} "
        f"manual={manual.withdrawal.id}"
    )


def dump(version: str) -> Path:
    """`--inserts` so the fixture loads without psql or COPY handling."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    target = FIXTURE_DIR / f"v{version}.sql"
    output = run(
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "postgres-test",
        "pg_dump",
        "--no-owner",
        "--no-privileges",
        "--no-comments",
        "--inserts",
        "--quote-all-identifiers",
        "-U",
        "cpapi",
        "-d",
        SCRATCH_DB,
    )
    header = (
        f"-- Frozen database fixture for v{version}.\n"
        "--\n"
        "-- Produced by scripts/make_upgrade_fixture.py. NEVER EDIT THIS FILE.\n"
        "-- Every future migration must be able to upgrade it; that is the only\n"
        "-- job it has. If a migration cannot, the migration is wrong.\n"
        "--\n"
    )
    target.write_text(header + output.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    log(f"wrote {target} ({target.stat().st_size // 1024} KB)")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="release version, e.g. 0.1.1")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))
    import os

    os.environ.setdefault("DATABASE_URL", SCRATCH_URL)
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("BITCOIN_NETWORK", "regtest")

    recreate_scratch_database()
    migrate_scratch_database()

    engine = create_engine(SCRATCH_URL)
    with sessionmaker(bind=engine, expire_on_commit=False, future=True)() as session:
        seed(session)
    engine.dispose()

    dump(args.version)
    log("done — commit the fixture and never touch it again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
