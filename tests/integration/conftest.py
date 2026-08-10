"""Integration fixtures: a real PostgreSQL 16, migrated by Alembic.

SQLite cannot stand in here. Deferred constraint triggers, partial unique
indexes, ON CONFLICT against a partial index and SELECT ... FOR UPDATE are not
incidental details of the ledger — they are the mechanisms under test.

Start the database with:

    docker compose -f deploy/docker-compose.test.yml up -d
"""

from __future__ import annotations

import uuid
from argparse import Namespace
from collections.abc import Iterator

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from crypto_processing_api.api.middleware import get_gateway
from crypto_processing_api.cli import asset_specs
from crypto_processing_api.config import get_settings
from crypto_processing_api.core import auth
from crypto_processing_api.ledger import service
from crypto_processing_api.ledger.invariants import assert_ledger_consistent
from crypto_processing_api.ledger.models import AccountKind, Base, EntryKind
from crypto_processing_api.main import create_app
from tests.conftest import (
    FAKE_STORE_ID,
    FAKE_WEBHOOK_SECRET,
    REPO_ROOT,
    TEST_DATABASE_URL,
)
from tests.fakes import FakeBTCPay

TestClientResponse = httpx.Response

BTC = "BTC"
USDT = "USDT_TRC20"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)


def make_alembic_config(url: str) -> Config:
    """An Alembic config pointed at `url`, with no credentials on disk.

    The url goes through `-x db_url=`, not `sqlalchemy.url`. migrations/env.py
    resolves the target itself and prefers DATABASE_URL over anything in the
    ini, so setting `sqlalchemy.url` here is silently ignored — a migration
    aimed at a scratch database would quietly run against the main one instead.
    `-x db_url=` is the override env.py checks first, and is the only one that
    actually steers it.
    """
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.cmd_opts = Namespace(x=[f"db_url={url}"])
    return config


@pytest.fixture(scope="session")
def alembic_ini() -> Config:
    return make_alembic_config(TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def engine(alembic_ini: Config) -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.fail(
            f"cannot reach the test database at {TEST_DATABASE_URL}: {exc}\n"
            "Start it with: docker compose -f deploy/docker-compose.test.yml up -d"
        )

    command.downgrade(alembic_ini, "base")
    command.upgrade(alembic_ini, "head")

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


@pytest.fixture
def fake_btcpay() -> FakeBTCPay:
    settings = get_settings()
    return FakeBTCPay(
        store_id=settings.btcpay_store_id or FAKE_STORE_ID,
        webhook_secret=settings.btcpay_webhook_secret or FAKE_WEBHOOK_SECRET,
    )


@pytest.fixture
def app(fake_btcpay: FakeBTCPay) -> Iterator[FastAPI]:
    """The real application with only the BTCPay gateway replaced."""
    application = create_app()
    application.dependency_overrides[get_gateway] = lambda: fake_btcpay
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # Not a context manager on purpose: running the lifespan would dispose the
    # engine the rest of the fixtures are still using.
    yield TestClient(app)


@pytest.fixture
def readwrite_key(session: Session) -> str:
    generated, _ = auth.create_api_key(
        session, name="platform", scope=auth.SCOPE_READWRITE, prefix=auth.KEY_PREFIX_TEST
    )
    session.commit()
    return generated.key


@pytest.fixture
def admin_key(session: Session) -> str:
    generated, _ = auth.create_api_key(
        session, name="ops", scope=auth.SCOPE_ADMIN, prefix=auth.KEY_PREFIX_TEST
    )
    session.commit()
    return generated.key


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def post_webhook(
    client: TestClient, fake: FakeBTCPay, payload: dict[str, object]
) -> TestClientResponse:
    """Send a webhook exactly as BTCPay would: raw bytes plus a real signature."""
    raw, headers = fake.sign(payload)
    return client.post("/webhooks/btcpay", content=raw, headers=headers)


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
