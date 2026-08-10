"""Process configuration.

Environment is the only configuration source (`.env.example` is the reference).
Validation is deliberately fail-fast: a misconfigured custodial service should
refuse to start rather than run with a surprising default.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
FeeMode = Literal["deduct", "absorb"]
BitcoinNetwork = Literal["mainnet", "testnet", "signet", "regtest"]
TronNetwork = Literal["mainnet", "nile"]

KEY_PREFIX_LIVE = "cpk_live_"
KEY_PREFIX_TEST = "cpk_test_"


def _empty_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


OptionalInt = Annotated[int | None, BeforeValidator(_empty_to_none)]
OptionalStr = Annotated[str | None, BeforeValidator(_empty_to_none)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Environment = "production"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_echo: bool = False

    api_host: str = "0.0.0.0"  # noqa: S104 — container-internal bind, never published
    api_port: int = 8000

    idempotency_stale_seconds: int = Field(default=60, ge=1)
    idempotency_ttl_hours: int = Field(default=72, ge=1)

    seed_btc_payment_method: str = "BTC-CHAIN"
    seed_btc_withdrawal_auto_limit: int = Field(default=500_000, ge=0)
    seed_btc_withdrawal_daily_cap: int = Field(default=5_000_000, ge=0)
    seed_btc_withdrawal_user_daily_cap: OptionalInt = None
    seed_btc_withdrawal_min: int = Field(default=1, ge=1)
    seed_btc_withdrawal_flat_fee: int = Field(default=0, ge=0)

    seed_usdt_payment_method: str = "USDT_TRC20"
    seed_usdt_withdrawal_auto_limit: int = Field(default=200_000_000, ge=0)
    seed_usdt_withdrawal_daily_cap: int = Field(default=2_000_000_000, ge=0)
    seed_usdt_withdrawal_user_daily_cap: OptionalInt = None
    seed_usdt_withdrawal_min: int = Field(default=1, ge=1)
    seed_usdt_withdrawal_flat_fee: int = Field(default=1_000_000, ge=0)

    withdrawal_fee_mode: FeeMode = "deduct"
    usdt_auto_withdraw: bool = False

    bitcoin_network: BitcoinNetwork = "mainnet"
    btc_fee_target_blocks: int = Field(default=3, ge=1)
    btc_payout_vsize_vb: int = Field(default=300, ge=1)
    btc_fallback_fee_sat_per_vb: int = Field(default=20, ge=1)
    btc_dust_threshold_sat: int = Field(default=546, ge=0)
    mempool_space_url: OptionalStr = "https://mempool.space/api/v1/fees/recommended"
    withdrawal_submit_interval_seconds: float = Field(default=10.0, gt=0)
    reconcile_withdrawal_interval_seconds: float = Field(default=60.0, gt=0)
    stuck_submitting_seconds: int = Field(default=300, ge=30)
    deposit_invoice_expiry_min_btc: int = Field(default=60, ge=1)
    deposit_invoice_expiry_min_usdt: int = Field(default=60, ge=1)
    deposit_monitoring_minutes: int = Field(default=1440, ge=1)

    webhook_poll_interval_seconds: float = Field(default=5.0, gt=0)
    webhook_max_attempts: int = Field(default=10, ge=1)
    reconcile_deposit_interval_seconds: float = Field(default=120.0, gt=0)
    reconcile_settled_window_days: int = Field(default=7, ge=1)
    reconcile_orphan_scan_interval_seconds: float = Field(default=86400.0, gt=0)
    wallet_scan_interval_seconds: float = Field(default=600.0, gt=0)

    btcpay_url: OptionalStr = None
    btcpay_api_key: OptionalStr = None
    btcpay_store_id: OptionalStr = None
    btcpay_webhook_secret: OptionalStr = None

    tron_network: TronNetwork = "mainnet"
    trongrid_base_url: OptionalStr = None
    trongrid_api_key: OptionalStr = None
    usdt_contract_address: OptionalStr = None
    tron_hot_wallet_address: OptionalStr = None
    tron_confirmations: int = Field(default=19, ge=1)
    gas_monitor_interval_seconds: float = Field(default=900.0, gt=0)
    trx_alert_threshold: int = Field(default=200, ge=0)
    usdt_amount_tolerance_pct: float = Field(default=1.0, ge=0)
    ntfy_topic_url: OptionalStr = None

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.debug and self.environment != "development":
            raise ValueError(
                "DEBUG=true is only allowed with ENVIRONMENT=development; "
                "debug mode leaks internals into responses and logs"
            )
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL psycopg 3 URL "
                "(postgresql:// or postgresql+psycopg://)"
            )
        if self.seed_btc_withdrawal_auto_limit > self.seed_btc_withdrawal_daily_cap:
            raise ValueError(
                "SEED_BTC_WITHDRAWAL_AUTO_LIMIT above SEED_BTC_WITHDRAWAL_DAILY_CAP would let a "
                "single withdrawal exceed the 24h cap"
            )
        if self.seed_usdt_withdrawal_auto_limit > self.seed_usdt_withdrawal_daily_cap:
            raise ValueError(
                "SEED_USDT_WITHDRAWAL_AUTO_LIMIT above SEED_USDT_WITHDRAWAL_DAILY_CAP would let a "
                "single withdrawal exceed the 24h cap"
            )
        if self.environment == "production" and self.bitcoin_network != "mainnet":
            raise ValueError(
                "BITCOIN_NETWORK is not mainnet in a production environment; withdrawal "
                "addresses would be validated against the wrong network"
            )
        # Configuring a TRON hot wallet without a TronGrid key is the mistake
        # worth catching: keyless access is throttled unpredictably, and the
        # thing being throttled is the check that a withdrawal really happened.
        if (
            self.environment == "production"
            and self.tron_hot_wallet_address
            and not self.trongrid_api_key
        ):
            raise ValueError(
                "TRON_HOT_WALLET_ADDRESS is set but TRONGRID_API_KEY is not; keyless "
                "TronGrid access is rate-limited unpredictably and USDT withdrawal "
                "verification depends on it"
            )
        if self.deposit_monitoring_minutes < self.deposit_invoice_expiry_min_btc:
            raise ValueError(
                "DEPOSIT_MONITORING_MINUTES below the invoice expiry means BTCPay stops "
                "attributing payments before the invoice is even expired"
            )
        return self

    @property
    def tron_endpoint(self) -> str:
        if self.trongrid_base_url:
            return self.trongrid_base_url
        return (
            "https://nile.trongrid.io" if self.tron_network == "nile" else "https://api.trongrid.io"
        )

    @property
    def usdt_contract(self) -> str:
        """The configured contract, or the default for this network.

        The Nile default is format-checked but not confirmed against a live
        node, so a Nile deployment should set USDT_CONTRACT_ADDRESS explicitly
        to whatever the USDt plugin is pointed at.
        """
        if self.usdt_contract_address:
            return self.usdt_contract_address
        return (
            "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf"
            if self.tron_network == "nile"
            else "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        )

    @property
    def tron_configured(self) -> bool:
        return bool(self.tron_hot_wallet_address)

    @property
    def btcpay_configured(self) -> bool:
        return bool(self.btcpay_url and self.btcpay_api_key and self.btcpay_store_id)

    @property
    def sqlalchemy_url(self) -> str:
        """Normalise to the psycopg 3 driver; SQLAlchemy defaults to psycopg2 otherwise."""
        if self.database_url.startswith("postgresql://"):
            return "postgresql+psycopg://" + self.database_url[len("postgresql://") :]
        return self.database_url

    @property
    def api_key_prefix(self) -> str:
        return KEY_PREFIX_TEST if self.environment in ("development", "test") else KEY_PREFIX_LIVE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
