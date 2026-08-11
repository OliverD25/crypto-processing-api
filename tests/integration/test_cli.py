"""The operator command line.

This is the only interface a deployment uses before it has an API key, so its
failure modes are the ones nobody can work around: a migration that does not
run, a key that is printed in a form the operator cannot pipe anywhere, or a
revoke that reports success without revoking anything.

The key-printing contract is asserted rather than described. Everything except
the key itself goes to stderr, so `... create-api-key | clip` yields the key and
nothing else — and a change that prints one word of prose to stdout silently
corrupts every key an operator stores that way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from crypto_processing_api.cli import asset_specs, main
from crypto_processing_api.config import get_settings
from crypto_processing_api.core import auth
from crypto_processing_api.ledger.models import ApiKey, Asset
from tests.conftest import REPO_ROOT

ALEMBIC_INI = str(REPO_ROOT / "alembic.ini")


# -- migrate ---------------------------------------------------------------


def test_migrate_reaches_head_and_reports_it(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate", "--config", ALEMBIC_INI]) == 0
    captured = capsys.readouterr()
    assert "schema at head" in captured.err


def test_a_second_migrate_leaves_the_seeded_values_alone(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    """The assets row is the source of truth after the first seed. An operator
    who edits a limit with SQL must not have it reverted by a redeploy."""
    asset = session.get(Asset, "BTC")
    assert asset is not None
    asset.withdrawal_auto_limit = 12_345
    session.commit()

    assert main(["migrate", "--config", ALEMBIC_INI]) == 0
    assert "already seeded" in capsys.readouterr().err

    session.expire_all()
    assert session.get(Asset, "BTC").withdrawal_auto_limit == 12_345  # type: ignore[union-attr]


@pytest.mark.no_ledger_check
def test_migrate_seeds_the_assets_a_fresh_database_lacks(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE assets RESTART IDENTITY CASCADE"))

    assert main(["migrate", "--config", ALEMBIC_INI]) == 0
    assert "seeded assets: BTC" in capsys.readouterr().err

    with engine.connect() as connection:
        seeded = set(connection.execute(text("SELECT id FROM assets")).scalars())
    assert {"BTC", "USDT_TRC20"} <= seeded


def test_migrate_can_skip_the_seed(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "--config", ALEMBIC_INI, "--no-seed"]) == 0
    captured = capsys.readouterr()
    assert "schema at head" in captured.err
    assert "seeded" not in captured.err


def test_migrate_refuses_a_config_that_is_not_there() -> None:
    """Told where to look and what to set, rather than an alembic stack trace."""
    with pytest.raises(SystemExit, match="alembic config not found"):
        main(["migrate", "--config", str(REPO_ROOT / "no-such-alembic.ini")])


def test_the_alembic_config_can_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ALEMBIC_CONFIG", ALEMBIC_INI)
    monkeypatch.chdir(REPO_ROOT.parent)
    assert main(["migrate", "--no-seed"]) == 0
    assert "schema at head" in capsys.readouterr().err


def test_the_seed_values_come_from_the_settings() -> None:
    """Read once, at migrate. Two places describing one limit is how a limit
    ends up silently disagreeing with itself."""
    specs = {spec.id: spec for spec in asset_specs(get_settings())}
    settings = get_settings()
    assert specs["BTC"].withdrawal_auto_limit == settings.seed_btc_withdrawal_auto_limit
    assert specs["USDT_TRC20"].withdrawal_flat_fee == settings.seed_usdt_withdrawal_flat_fee
    assert specs["USDT_TRC20"].pooled_addresses is True
    # Off by default, so a deployment that never asked for Lightning has no
    # BTC_LN row at all rather than a disabled one.
    assert "BTC_LN" not in specs


def test_lightning_adds_its_own_asset_with_its_own_expiry(lightning: None) -> None:
    specs = {spec.id: spec for spec in asset_specs(get_settings())}
    assert specs["BTC_LN"].pooled_addresses is False
    assert specs["BTC_LN"].deposit_expiry_minutes == get_settings().deposit_invoice_expiry_min_ln


# -- create-api-key --------------------------------------------------------


def test_create_api_key_prints_only_the_key_on_stdout(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["create-api-key", "--name", "platform", "--scope", "readwrite"]) == 0
    captured = capsys.readouterr()

    key = captured.out.strip()
    assert captured.out == f"{key}\n"
    assert key.startswith(auth.KEY_PREFIX_TEST)

    authenticated = auth.authenticate(session, key)
    assert authenticated.name == "platform"
    assert authenticated.scope == auth.SCOPE_READWRITE


def test_create_api_key_says_on_stderr_that_it_cannot_be_shown_again(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["create-api-key", "--name", "ops", "--scope", "admin"])
    captured = capsys.readouterr()
    assert "only time the key is shown" in captured.err
    assert "SHA-256" in captured.err
    assert "expires never" in captured.err
    # The key_id is the safe-to-log handle, and it is what revoke takes.
    key_id = auth.parse_api_key(captured.out.strip()).key_id
    assert key_id in captured.err
    # The secret half must not be repeated into the prose stream.
    assert captured.out.strip() not in captured.err


def test_an_expiry_is_stored_and_reported(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["create-api-key", "--name", "temp", "--scope", "readwrite", "--expires-days", "30"])
        == 0
    )
    captured = capsys.readouterr()
    key_id = auth.parse_api_key(captured.out.strip()).key_id

    record = session.execute(select(ApiKey).where(ApiKey.key_id == key_id)).scalar_one()
    assert record.expires_at is not None
    remaining = record.expires_at - datetime.now(UTC)
    assert 29 <= remaining.days <= 30
    assert record.expires_at.isoformat()[:10] in captured.err


def test_an_expired_key_no_longer_authenticates(
    session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["create-api-key", "--name", "temp", "--scope", "readwrite", "--expires-days", "1"])
    key = capsys.readouterr().out.strip()
    later = datetime.now(UTC).replace(year=datetime.now(UTC).year + 2)
    with pytest.raises(auth.ExpiredApiKey):
        auth.authenticate(session, key, now=later)


def test_an_unknown_scope_is_refused_by_the_parser() -> None:
    """argparse, not the service: a typo must not reach create_api_key."""
    with pytest.raises(SystemExit) as caught:
        main(["create-api-key", "--name", "x", "--scope", "superuser"])
    assert caught.value.code == 2


def test_the_name_is_required() -> None:
    with pytest.raises(SystemExit):
        main(["create-api-key", "--scope", "admin"])


# -- revoke-api-key --------------------------------------------------------


def test_revoke_stops_the_key_working(session: Session, capsys: pytest.CaptureFixture[str]) -> None:
    main(["create-api-key", "--name", "leaked", "--scope", "admin"])
    key = capsys.readouterr().out.strip()
    key_id = auth.parse_api_key(key).key_id

    assert main(["revoke-api-key", "--key-id", key_id]) == 0
    assert f"revoked {key_id}" in capsys.readouterr().err

    session.expire_all()
    with pytest.raises(auth.RevokedApiKey):
        auth.authenticate(session, key)


def test_revoking_an_unknown_key_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """A script that revokes on a schedule has to be able to tell the two apart."""
    unknown = uuid.uuid4().hex[:8]
    assert main(["revoke-api-key", "--key-id", unknown]) == 1
    assert f"no active key with key_id {unknown}" in capsys.readouterr().err


def test_revoking_twice_reports_the_second_one_as_a_miss(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["create-api-key", "--name", "leaked", "--scope", "admin"])
    key_id = auth.parse_api_key(capsys.readouterr().out.strip()).key_id
    assert main(["revoke-api-key", "--key-id", key_id]) == 0
    assert main(["revoke-api-key", "--key-id", key_id]) == 1


# -- parser ----------------------------------------------------------------


def test_a_bare_invocation_is_refused() -> None:
    """No default command: `migrate` must always be asked for by name."""
    with pytest.raises(SystemExit):
        main([])


def test_an_unknown_command_is_refused() -> None:
    with pytest.raises(SystemExit):
        main(["seed-everything"])
