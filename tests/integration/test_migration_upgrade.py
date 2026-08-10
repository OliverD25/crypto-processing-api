"""Every frozen release dump must still upgrade cleanly.

`tests/fixtures/upgrade/` holds one dump per release, each a real database from
that version, each never edited again. A migration that cannot get past one of
them is a migration that would break somebody's deployment.

This catches the thing a fresh-database test cannot: a migration that is fine
on empty tables and wrong on data. `ALTER TABLE ... SET NOT NULL` on a column
with historical NULLs, a backfill with the wrong default, a rewrite of
`postings` — the append-only triggers reject that last one outright, which is
the invariant doing the test's work for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.ledger.invariants import assert_ledger_consistent, custody_reports
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Deposit,
    JournalEntry,
    Withdrawal,
    WithdrawalStatus,
)
from tests.conftest import REPO_ROOT, TEST_DATABASE_URL
from tests.integration.conftest import make_alembic_config

pytestmark = pytest.mark.integration

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "upgrade"
UPGRADE_DB = "cpapi_upgrade_test"

#: Frozen expectations, in smallest units. These come from the scenario in
#: scripts/make_upgrade_fixture.py and must survive every future migration.
EXPECTED_BTC = {
    # 500_000_000 deposited, 10_000_000 withdrawn and confirmed.
    ("fixture-user-a", AccountKind.USER_AVAILABLE): 490_000_000,
    ("fixture-user-a", AccountKind.USER_HOLD): 0,
    # 120_000_000 deposited, 60_000_000 held by a withdrawal awaiting approval.
    ("fixture-user-b", AccountKind.USER_AVAILABLE): 60_000_000,
    ("fixture-user-b", AccountKind.USER_HOLD): 60_000_000,
}
EXPECTED_USDT = {
    # 900_000_000 deposited, 200_000_000 held by the manual withdrawal.
    ("fixture-user-a", AccountKind.USER_AVAILABLE): 700_000_000,
    ("fixture-user-a", AccountKind.USER_HOLD): 200_000_000,
}


def fixtures() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("v*.sql")) if FIXTURE_DIR.is_dir() else []


def executable_sql(fixture: Path) -> str:
    """The dump with psql meta-commands removed.

    pg_dump wraps its output in `\\restrict` / `\\unrestrict` — a psql directive,
    not SQL, that stops a hostile object name from smuggling meta-commands into
    a restore. We load through psycopg rather than psql, so the lines are both
    unnecessary and a syntax error. They are stripped here instead of at
    generation time on purpose: the fixture on disk stays a byte-exact pg_dump
    artifact, and the guard token is random per dump anyway.
    """
    lines = fixture.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("\\"))


def load_fixture(session: Session, fixture: Path) -> None:
    """Restore a dump into the session's database.

    The search_path reset at the end is not decoration. pg_dump opens with
    `set_config('search_path', '', false)` and schema-qualifies everything it
    writes. That third argument is `is_local`, so the empty search_path belongs
    to the *session*, not the transaction, and a commit or rollback will not put
    it back. Every later unqualified query on this connection would then fail to
    resolve a table that is demonstrably there.
    """
    connection = session.connection().connection
    with connection.cursor() as cursor:
        cursor.execute(executable_sql(fixture))
    session.commit()
    session.execute(text("SET search_path TO public"))
    session.commit()


def _admin_url() -> str:
    return TEST_DATABASE_URL.rsplit("/", 1)[0] + "/cpapi_test"


def _upgrade_url() -> str:
    return TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{UPGRADE_DB}"


@pytest.fixture
def scratch_database() -> Session:
    """A database of its own, so a fixture load cannot disturb the shared one."""
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{UPGRADE_DB}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{UPGRADE_DB}"'))
    admin.dispose()

    engine = create_engine(_upgrade_url())
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{UPGRADE_DB}" WITH (FORCE)'))
        admin.dispose()


def balance(session: Session, asset: str, user: str, kind: AccountKind) -> int:
    account = session.execute(
        select(Account).where(
            Account.asset_id == asset,
            Account.kind == kind,
            Account.external_user_id == user,
        )
    ).scalar_one_or_none()
    return 0 if account is None else -account.balance


def test_there_is_at_least_one_frozen_fixture() -> None:
    """A release checklist item that silently stops happening is worthless."""
    assert fixtures(), (
        "tests/fixtures/upgrade/ is empty. Every release adds a dump — see "
        "scripts/make_upgrade_fixture.py and the checklist in CONTRIBUTING.md"
    )


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda path: path.stem)
def test_a_frozen_database_upgrades_to_head(fixture: Path, scratch_database: Session) -> None:
    session = scratch_database
    load_fixture(session, fixture)

    command.upgrade(make_alembic_config(_upgrade_url()), "head")
    session.close()

    # Everything below reads the upgraded database.
    assert_ledger_consistent(session)

    for (user, kind), expected in EXPECTED_BTC.items():
        assert balance(session, "BTC", user, kind) == expected, (
            f"BTC {kind.value} for {user} changed across the upgrade"
        )
    for (user, kind), expected in EXPECTED_USDT.items():
        assert balance(session, "USDT_TRC20", user, kind) == expected, (
            f"USDT {kind.value} for {user} changed across the upgrade"
        )

    # The books still balance per asset.
    for report in custody_reports(session):
        assert report.balanced, f"{report.asset_id} residual {report.residual}"

    # And the rows a migration is most likely to mishandle are all still there.
    assert session.execute(select(Deposit)).scalars().all(), "the settled deposit vanished"
    statuses = {row.status for row in session.execute(select(Withdrawal)).scalars()}
    assert WithdrawalStatus.CONFIRMED in statuses
    assert WithdrawalStatus.PENDING_APPROVAL in statuses
    backends = {row.backend for row in session.execute(select(Withdrawal)).scalars()}
    assert "manual_tron" in backends, "the manual-TRON withdrawal vanished"


@pytest.mark.parametrize("fixture", fixtures(), ids=lambda path: path.stem)
def test_the_journal_survived_untouched(fixture: Path, scratch_database: Session) -> None:
    """A migration that rewrites history would be caught by the triggers.

    This asserts the softer version: the entries are all still present after the
    upgrade, in the same number the fixture froze.
    """
    session = scratch_database
    load_fixture(session, fixture)

    before = session.execute(select(JournalEntry.id).order_by(JournalEntry.id)).scalars().all()

    command.upgrade(make_alembic_config(_upgrade_url()), "head")
    session.close()

    after = session.execute(select(JournalEntry.id).order_by(JournalEntry.id)).scalars().all()
    assert after == before, "a migration added, removed or renumbered journal entries"
