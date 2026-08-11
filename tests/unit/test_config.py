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


def test_lightning_auto_limit_above_its_own_daily_cap_refused() -> None:
    """Channel balance is a second pot of money; the BTC caps do not bound it."""
    with pytest.raises(ValidationError, match="exceed the 24h cap"):
        make_settings(
            seed_ln_withdrawal_auto_limit=3_000_000,
            seed_ln_withdrawal_daily_cap=2_000_000,
        )


def test_production_refuses_a_test_bitcoin_network() -> None:
    """Otherwise withdrawal addresses are validated against the wrong chain."""
    with pytest.raises(ValidationError, match="not mainnet in a production"):
        make_settings(environment="production", bitcoin_network="regtest")


@pytest.mark.parametrize("network", ["testnet", "signet", "regtest"])
def test_every_non_mainnet_network_is_refused_in_production(network: str) -> None:
    with pytest.raises(ValidationError, match="not mainnet in a production"):
        make_settings(environment="production", bitcoin_network=network)


def test_a_test_network_is_fine_outside_production() -> None:
    assert make_settings(environment="development", bitcoin_network="regtest").bitcoin_network == (
        "regtest"
    )


def test_a_tron_hot_wallet_without_a_trongrid_key_is_refused_in_production() -> None:
    """Keyless TronGrid is throttled unpredictably, and what gets throttled is
    the check that a USDT withdrawal really happened."""
    with pytest.raises(ValidationError, match="TRONGRID_API_KEY"):
        make_settings(
            environment="production",
            bitcoin_network="mainnet",
            tron_hot_wallet_address="TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
        )


def test_a_keyless_tron_wallet_is_allowed_outside_production() -> None:
    settings = make_settings(
        environment="development",
        tron_hot_wallet_address="TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
    )
    assert settings.tron_configured is True


def test_usdt_auto_withdraw_is_refused_because_nothing_could_send_it() -> None:
    """`place_hold` would create the row already approved, and the only code
    that hands a manual withdrawal to an operator requires pending_approval."""
    with pytest.raises(ValidationError, match="no payout handler"):
        make_settings(usdt_auto_withdraw=True)


def test_an_outbound_url_without_a_secret_is_refused() -> None:
    """An unsigned webhook is one anybody can forge."""
    with pytest.raises(ValidationError, match="PLATFORM_WEBHOOK_SECRET"):
        make_settings(platform_webhook_url="https://platform.test/hooks")


def test_an_outbound_secret_without_a_url_is_simply_off() -> None:
    settings = make_settings(platform_webhook_secret="s3cret")
    assert settings.outbound_configured is False


def test_a_configured_outbound_pair_reads_as_configured() -> None:
    settings = make_settings(
        platform_webhook_url="https://platform.test/hooks", platform_webhook_secret="s3cret"
    )
    assert settings.outbound_configured is True


def test_monitoring_shorter_than_the_invoice_expiry_is_refused() -> None:
    """BTCPay would stop attributing payments before the invoice even expired."""
    with pytest.raises(ValidationError, match="below the invoice expiry"):
        make_settings(deposit_monitoring_minutes=30, deposit_invoice_expiry_min_btc=60)


@pytest.mark.parametrize(
    ("network", "endpoint"),
    [("mainnet", "https://api.trongrid.io"), ("nile", "https://nile.trongrid.io")],
)
def test_the_tron_endpoint_follows_the_network(network: str, endpoint: str) -> None:
    assert make_settings(tron_network=network).tron_endpoint == endpoint


def test_an_explicit_tron_endpoint_wins() -> None:
    settings = make_settings(tron_network="nile", trongrid_base_url="http://tron.internal:8090")
    assert settings.tron_endpoint == "http://tron.internal:8090"


@pytest.mark.parametrize(
    ("network", "contract"),
    [
        ("mainnet", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"),
        ("nile", "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"),
    ],
)
def test_the_usdt_contract_follows_the_network(network: str, contract: str) -> None:
    assert make_settings(tron_network=network).usdt_contract == contract


def test_an_explicit_usdt_contract_wins() -> None:
    """The Nile default is format-checked only, so a Nile deployment overrides it."""
    other = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"
    assert make_settings(tron_network="nile", usdt_contract_address=other).usdt_contract == other


@pytest.mark.parametrize(
    ("url", "key", "store", "configured"),
    [
        ("http://btcpay.test", "k", "s", True),
        (None, "k", "s", False),
        ("http://btcpay.test", None, "s", False),
        ("http://btcpay.test", "k", None, False),
    ],
)
def test_btcpay_is_configured_only_when_all_three_are_set(
    url: str | None, key: str | None, store: str | None, configured: bool
) -> None:
    settings = make_settings(btcpay_url=url, btcpay_api_key=key, btcpay_store_id=store)
    assert settings.btcpay_configured is configured


@pytest.mark.parametrize(
    ("environment", "prefix"),
    [
        ("development", KEY_PREFIX_TEST),
        ("test", KEY_PREFIX_TEST),
        ("staging", KEY_PREFIX_LIVE),
        ("production", KEY_PREFIX_LIVE),
    ],
)
def test_the_key_prefix_says_which_deployment_minted_it(environment: str, prefix: str) -> None:
    settings = make_settings(environment=environment, bitcoin_network="mainnet")
    assert settings.api_key_prefix == prefix


def test_shipping_defaults_match_the_plan() -> None:
    settings = make_settings()
    assert settings.seed_btc_withdrawal_auto_limit == 500_000
    assert settings.seed_btc_withdrawal_daily_cap == 5_000_000
    assert settings.seed_usdt_withdrawal_auto_limit == 200_000_000
    assert settings.seed_usdt_withdrawal_daily_cap == 2_000_000_000
    assert settings.seed_usdt_withdrawal_flat_fee == 1_000_000
    assert settings.withdrawal_fee_mode == "deduct"
    assert settings.usdt_auto_withdraw is False
