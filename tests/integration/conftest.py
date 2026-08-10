"""Integration fixtures: a real PostgreSQL 16, migrated by Alembic.

SQLite cannot stand in here. Deferred constraint triggers, partial unique
indexes, ON CONFLICT against a partial index and SELECT ... FOR UPDATE are not
incidental details of the ledger — they are the mechanisms under test.

Start the database with:

    docker compose -f deploy/docker-compose.test.yml up -d
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.cli import asset_specs
from crypto_processing_api.config import get_settings
from crypto_processing_api.ledger import service
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import AccountKind, Base, EntryKind
from tests.conftest import REPO_ROOT, TEST_DATABASE_URL

BTC = "BTC"
USDT = "USDT_TRC20"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.fail(
            f"cannot reach the test database at {TEST_DATABASE_URL}: {exc}\n"
            "Start it with: docker compose -f deploy/docker-compose.test.yml up -d"
        )

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def clean_database(engine: Engine) -> Iterator[None]:
    """Empty every table, then seed the assets and their system accounts.

    TRUNCATE rather than DELETE: the append-only triggers on postings and
    journal_entries reject row-level DELETE, which is exactly the point of
    them.
    """
    tables = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        service.seed_assets(session, asset_specs(get_settings()))
        session.commit()
    yield


@pytest.fixture(autouse=True)
def ledger_stays_consistent(
    request: pytest.FixtureRequest, engine: Engine, clean_database: None
) -> Iterator[None]:
    """Assert the books hold together after every scenario.

    Mark a test with `@pytest.mark.no_ledger_check` when it deliberately leaves
    the ledger broken in order to prove a detector works.
    """
    yield
    if request.node.get_closest_marker("no_ledger_check"):
        return
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        assert_ledger_consistent(session)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def user_id() -> str:
    return f"u-{uuid.uuid4().hex[:12]}"


def credit_user(session: Session, *, user: str, amount: int, asset: str = BTC) -> None:
    """Put money on a user's available balance exactly the way a deposit does."""
    hot = service.get_system_account(session, asset_id=asset, kind=AccountKind.HOT_WALLET)
    available, _ = service.get_user_accounts(session, asset_id=asset, external_user_id=user)
    service.post_entry(
        session,
        kind=EntryKind.DEPOSIT_CREDIT,
        asset_id=asset,
        source_ref=f"btcpay_payment:{uuid.uuid4().hex}:0",
        postings=[(hot.id, amount), (available.id, -amount)],
    )
    session.commit()
