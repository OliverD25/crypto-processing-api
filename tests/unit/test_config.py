"""Settings validation: a misconfigured custodial service must not start."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crypto_processing_api.config import KEY_PREFIX_LIVE, KEY_PREFIX_TEST, Settings

BASE_URL = "postgresql://cpapi:cpapi@localhost:5432/cpapi"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every settings variable so these tests see real defaults.

    The test suite exports DATABASE_URL and ENVIRONMENT for everything else,
    and pydantic-settings reads the process environment whatever `_env_file`
    says.
    """
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"database_url": BASE_URL, "_env_file": None}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_defaults_are_production_safe() -> None:
    settings = make_settings()
    assert settings.environment == "production"
    assert settings.debug is False
    assert settings.log_json is True
    assert settings.api_key_prefix == KEY_PREFIX_LIVE


def test_debug_refused_outside_development() -> None:
    with pytest.raises(ValidationError, match="ENVIRONMENT=development"):
        make_settings(debug=True, environment="production")


def test_debug_allowed_in_development() -> None:
    settings = make_settings(debug=True, environment="development")
    assert settings.debug is True
    assert settings.api_key_prefix == KEY_PREFIX_TEST


def test_non_postgres_url_refused() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        make_settings(database_url="sqlite:///ledger.db")


def test_missing_database_url_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_url_is_normalised_to_psycopg() -> None:
    settings = make_settings()
    assert settings.sqlalchemy_url == "postgresql+psycopg://cpapi:cpapi@localhost:5432/cpapi"


def test_explicit_psycopg_url_is_left_alone() -> None:
    url = "postgresql+psycopg://cpapi:cpapi@localhost:5432/cpapi"
    assert make_settings(database_url=url).sqlalchemy_url == url


def test_auto_limit_above_daily_cap_refused() -> None:
    with pytest.raises(ValidationError, match="exceed the 24h cap"):
        make_settings(
            seed_btc_withdrawal_auto_limit=10_000_000,
            seed_btc_withdrawal_daily_cap=5_000_000,
        )


def test_usdt_auto_limit_above_daily_cap_refused() -> None:
    with pytest.raises(ValidationError, match="exceed the 24h cap"):
        make_settings(
            seed_usdt_withdrawal_auto_limit=3_000_000_000,
            seed_usdt_withdrawal_daily_cap=2_000_000_000,
        )


def test_empty_optional_cap_becomes_none() -> None:
    assert (
        make_settings(seed_btc_withdrawal_user_daily_cap="").seed_btc_withdrawal_user_daily_cap
        is None
    )


def test_shipping_defaults_match_the_plan() -> None:
    settings = make_settings()
    assert settings.seed_btc_withdrawal_auto_limit == 500_000
    assert settings.seed_btc_withdrawal_daily_cap == 5_000_000
    assert settings.seed_usdt_withdrawal_auto_limit == 200_000_000
    assert settings.seed_usdt_withdrawal_daily_cap == 2_000_000_000
    assert settings.seed_usdt_withdrawal_flat_fee == 1_000_000
    assert settings.withdrawal_fee_mode == "deduct"
    assert settings.usdt_auto_withdraw is False
