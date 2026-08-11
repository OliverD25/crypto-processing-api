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

    # === Process ===

    #: Also selects the API key prefix: `development` and `test` mint `cpk_test_`
    #: keys, everything else mints `cpk_live_` ones.
    environment: Environment = "production"
    #: Verbose errors and internals in responses. Refused unless
    #: `ENVIRONMENT=development`.
    debug: bool = False
    #: Python logging level for the structlog pipeline.
    log_level: str = "INFO"
    #: One JSON object per line for a log collector; `false` gives human-readable
    #: console output.
    log_json: bool = True

    # === Database ===

    #: The ledger. Never the same instance as BTCPay's own database.
    database_url: str
    #: Postgres connections are the scarce resource on a 4GB box shared with
    #: BTCPay and bitcoind, so both pool numbers stay small.
    db_pool_size: int = Field(default=5, ge=1)
    #: Connections opened beyond the pool when it is exhausted.
    db_max_overflow: int = Field(default=5, ge=0)
    #: Echo every SQL statement. Debugging only, and extremely noisy.
    db_echo: bool = False

    # === HTTP server ===

    #: Bind address inside the container. The API publishes no ports in
    #: production; BTCPay and the platform reach it over the Docker network.
    api_host: str = "0.0.0.0"  # noqa: S104 — container-internal bind, never published
    #: Port the API listens on inside the container.
    api_port: int = 8000

    # === Idempotency-Key handling ===

    #: An `in_progress` idempotency row older than this may be reclaimed by a
    #: retry, so a crash between "row inserted" and "work done" cannot turn into
    #: a permanent 409 on a money endpoint.
    idempotency_stale_seconds: int = Field(default=60, ge=1)
    #: How long completed idempotency records are kept before the purge job
    #: removes them.
    idempotency_ttl_hours: int = Field(default=72, ge=1)

    # === Asset seed values — BTC (satoshis) ===
    #
    # Every `SEED_*` value is read once, by `cli migrate`, to insert the `assets`
    # rows. After that the database row is the only source of truth and editing
    # the variable changes nothing.

    #: BTCPay's payment method id for on-chain BTC. Version-dependent; the real
    #: value is discovered from the store at startup.
    seed_btc_payment_method: str = "BTC-CHAIN"
    #: Withdrawals at or below this gross amount approve themselves. Above it
    #: they queue for an admin.
    seed_btc_withdrawal_auto_limit: int = Field(default=500_000, ge=0)
    #: Rolling 24-hour per-asset cap. Once the window sum reaches it every
    #: withdrawal goes to manual approval regardless of size — the control that
    #: actually bounds a stolen-API-key attack.
    seed_btc_withdrawal_daily_cap: int = Field(default=5_000_000, ge=0)
    #: Optional rolling 24-hour cap per user. Unset disables it.
    seed_btc_withdrawal_user_daily_cap: OptionalInt = None
    #: Smallest accepted gross withdrawal. The dust rule applies on top.
    seed_btc_withdrawal_min: int = Field(default=1, ge=1)
    #: Flat service fee charged on top of the estimated miner fee.
    seed_btc_withdrawal_flat_fee: int = Field(default=0, ge=0)

    # === Asset seed values — Lightning, BTC_LN (satoshis) ===

    #: Lightning is off unless a deployment asks for it, and the default is the
    #: security decision rather than a convenience. Enabling it means the
    #: bootstrap requests `btcpay.server.canuseinternallightningnode`, which is
    #: a **server-level** BTCPay permission — the one kind this project
    #: otherwise refuses to hold. See [the security model](../operating/security.md),
    #: threat 5.
    lightning_enabled: bool = False
    #: BTCPay's Lightning payment method id.
    seed_ln_payment_method: str = "BTC-LN"
    #: The same meaning as the BTC caps, over a different pot of money: channel
    #: balance, which the BTC caps say nothing about.
    seed_ln_withdrawal_auto_limit: int = Field(default=200_000, ge=0)
    #: Rolling 24-hour cap on the Lightning float, in satoshis.
    seed_ln_withdrawal_daily_cap: int = Field(default=2_000_000, ge=0)
    #: Optional rolling 24-hour cap per user. Unset disables it.
    seed_ln_withdrawal_user_daily_cap: OptionalInt = None
    #: Above the flat fee on purpose: a minimum below it would accept requests
    #: that deliver nothing and be refused as dust a moment later.
    seed_ln_withdrawal_min: int = Field(default=1_000, ge=1)
    #: A flat service charge, because a routing fee cannot be estimated before
    #: the route is found. What the route actually cost is journalled at settle
    #: time from the node, so this number is revenue and not a cost estimate.
    seed_ln_withdrawal_flat_fee: int = Field(default=100, ge=0)
    #: Shorter than the on-chain window on purpose: an open BOLT11 invoice ties
    #: up inbound channel liquidity for as long as it lives.
    deposit_invoice_expiry_min_ln: int = Field(default=30, ge=1)
    #: How long a Lightning payout may sit in BTCPay's `AwaitingPayment` before
    #: this service cancels it. BTCPay's Lightning processor retries a payout it
    #: cannot route for as long as the invoice lives and never moves it to a
    #: failed state, so without a timeout the withdrawal stays `submitted`
    #: forever with the user's balance held.
    ln_payout_timeout_seconds: int = Field(default=900, ge=60)

    # === Asset seed values — USDT-TRC20 (micro-USDT) ===

    #: BTCPay's payment method id from the USDt plugin. Version-dependent;
    #: discovered from the store at startup like the BTC one.
    seed_usdt_payment_method: str = "USDT_TRC20"
    #: USDT withdrawals are manual-only today, so this limit only starts to
    #: matter once an automated TRON signer exists.
    seed_usdt_withdrawal_auto_limit: int = Field(default=200_000_000, ge=0)
    #: Rolling 24-hour per-asset cap, in micro-USDT.
    seed_usdt_withdrawal_daily_cap: int = Field(default=2_000_000_000, ge=0)
    #: Optional rolling 24-hour cap per user. Unset disables it.
    seed_usdt_withdrawal_user_daily_cap: OptionalInt = None
    #: Smallest accepted gross withdrawal, in micro-USDT.
    seed_usdt_withdrawal_min: int = Field(default=1, ge=1)
    #: Flat fee covering the operator's TRX gas for a TRC-20 transfer.
    seed_usdt_withdrawal_flat_fee: int = Field(default=1_000_000, ge=0)

    # === Withdrawal policy ===

    #: `deduct` — the user receives gross minus the fee. `absorb` — the user
    #: receives gross and the operator pays the fee.
    withdrawal_fee_mode: FeeMode = "deduct"
    #: Refused at startup while it is `true`: the BTCPay USDt plugin has no
    #: payout handler, so nothing could send an auto-approved USDT withdrawal.
    usdt_auto_withdraw: bool = False

    # === Bitcoin network, fee estimation and deposit windows ===

    #: Which network destination addresses are validated against. A production
    #: environment refuses to start on anything but `mainnet`.
    bitcoin_network: BitcoinNetwork = "mainnet"
    #: Confirmation target for the fee estimate, in blocks.
    btc_fee_target_blocks: int = Field(default=3, ge=1)
    #: Assumed payout size in virtual bytes. Raise it if the hot wallet holds
    #: many small deposit UTXOs; each extra P2WPKH input is roughly 68 vB.
    btc_payout_vsize_vb: int = Field(default=300, ge=1)
    #: Used only when BTCPay and mempool.space both fail to answer. Reaching it
    #: logs an error, because every withdrawal is then priced off a guess.
    btc_fallback_fee_sat_per_vb: int = Field(default=20, ge=1)
    #: A withdrawal whose net amount lands at or below this is refused before
    #: any hold is placed; the output would be unspendable.
    btc_dust_threshold_sat: int = Field(default=546, ge=0)
    #: Second fee source, tried when BTCPay's wallet estimate is unavailable.
    #: Unset disables it, which is right for a deployment that must make no
    #: outbound calls to third parties.
    mempool_space_url: OptionalStr = "https://mempool.space/api/v1/fees/recommended"
    #: How often approved withdrawals are turned into BTCPay payouts.
    withdrawal_submit_interval_seconds: float = Field(default=10.0, gt=0)
    #: Job B: how often submitted and broadcast payouts are re-checked.
    reconcile_withdrawal_interval_seconds: float = Field(default=60.0, gt=0)
    #: How long a withdrawal may sit in `submitting` with no payout reference
    #: before reconciliation works out what happened to it.
    stuck_submitting_seconds: int = Field(default=300, ge=30)
    #: Invoice checkout window. USDT is deliberately not shortened to recycle
    #: the address pool: a short window makes a late payment land on another
    #: user's reserved address. Size the pool up instead.
    deposit_invoice_expiry_min_btc: int = Field(default=60, ge=1)
    #: The USDT checkout window. See the note on the BTC one above.
    deposit_invoice_expiry_min_usdt: int = Field(default=60, ge=1)
    #: How long after expiry BTCPay keeps attributing payments to an invoice,
    #: and therefore how long the deposit sweep keeps asking. Must be at least
    #: the invoice expiry.
    deposit_monitoring_minutes: int = Field(default=1440, ge=1)

    # === Worker schedule (the reconciliation loop) ===

    #: How often the worker drains the `webhook_events` queue.
    webhook_poll_interval_seconds: float = Field(default=5.0, gt=0)
    #: Attempts before an inbound webhook event is parked for an operator.
    webhook_max_attempts: int = Field(default=10, ge=1)
    #: Job A: re-ask BTCPay about every deposit still worth asking about.
    reconcile_deposit_interval_seconds: float = Field(default=120.0, gt=0)
    #: Fallback window for polling settled and expired deposits when BTCPay
    #: reported no monitoring expiry.
    reconcile_settled_window_days: int = Field(default=7, ge=1)
    #: Nightly page-through of the store's invoices looking for ours that have
    #: no local row — the case after restoring an older database backup.
    reconcile_orphan_scan_interval_seconds: float = Field(default=86400.0, gt=0)
    #: Wallet scan: flags coins that reached the hot wallet matching no deposit
    #: payment. The only detector for a payment to an address BTCPay stopped
    #: watching.
    wallet_scan_interval_seconds: float = Field(default=600.0, gt=0)

    # === BTCPay Server ===

    #: Greenfield base URL, container-to-container inside the Docker network.
    btcpay_url: OptionalStr = None
    #: Restricted Greenfield API key scoped to the single store. Never
    #: server-admin.
    btcpay_api_key: OptionalStr = None
    #: The store this service owns.
    btcpay_store_id: OptionalStr = None
    #: Webhook signing secret. Must differ from `BTCPAY_API_KEY` — the ingress
    #: path verifies an HMAC over raw bytes with this value.
    btcpay_webhook_secret: OptionalStr = None

    # === TRON / USDT-TRC20 ===

    #: Selects the default TronGrid endpoint and the default USDT contract.
    tron_network: TronNetwork = "mainnet"
    #: Override the endpoint for your own node or a paid provider. Unset uses
    #: the network's TronGrid default.
    trongrid_base_url: OptionalStr = None
    #: Required in production once a hot wallet address is set: keyless
    #: TronGrid access is throttled unpredictably, and what gets throttled is
    #: the check that a withdrawal really happened.
    trongrid_api_key: OptionalStr = None
    #: Must match what the USDt plugin watches. The withdrawal verifier refuses
    #: a transfer of any other token. Unset uses the network default; both
    #: defaults were read off their chains on 2026-08-11 and answered `USDT` /
    #: `6`, so setting this is about agreeing with your plugin rather than
    #: about distrusting the default.
    usdt_contract_address: OptionalStr = None
    #: The address USDT is sent from. Verification compares every transfer's
    #: sender against it, so a wrong value rejects every genuine withdrawal.
    #: Unset disables the TRON jobs entirely, which is right for a BTC-only
    #: deployment.
    tron_hot_wallet_address: OptionalStr = None
    #: Blocks before a manual USDT withdrawal is treated as final. 19 is
    #: roughly TRON's solidified-block distance.
    tron_confirmations: int = Field(default=19, ge=1)
    #: How often the TRX gas balance of the hot wallet is checked.
    gas_monitor_interval_seconds: float = Field(default=900.0, gt=0)
    #: TRX balance below which an alert is raised. A wallet full of USDT and
    #: empty of TRX cannot send anything, and the symptom reads as "withdrawals
    #: are broken".
    trx_alert_threshold: int = Field(default=200, ge=0)
    #: How far a settled USDT deposit may deviate from the expected amount
    #: before it goes to review instead of crediting. Applies only when the
    #: platform supplied an expected amount; 0 disables it.
    usdt_amount_tolerance_pct: float = Field(default=1.0, ge=0)

    # === Alerts ===

    #: Optional ntfy.sh (or compatible) topic URL. Alerts are always logged;
    #: this adds a push.
    ntfy_topic_url: OptionalStr = None
    #: Optional Telegram bot. Both the token and the chat id must be set or the
    #: transport stays off.
    telegram_bot_token: OptionalStr = None
    #: The chat alerts are sent to. Useless without the bot token.
    telegram_chat_id: OptionalStr = None

    # === Outbound webhooks to the platform ===

    #: Where deposit and withdrawal events are POSTed. Unset disables delivery,
    #: and events queue rather than being lost, so turning it on later ships
    #: the backlog.
    platform_webhook_url: OptionalStr = None
    #: HMAC-SHA256 key for the `X-CPA-Signature` header. The service refuses to
    #: start with a URL set and this unset: an unsigned webhook is one anybody
    #: can forge.
    platform_webhook_secret: OptionalStr = None
    #: How often the delivery worker drains the queue. The per-event retry
    #: schedule is fixed in code and is not configurable.
    outbound_delivery_interval_seconds: float = Field(default=15.0, gt=0)
    #: Per-request timeout for one delivery attempt.
    outbound_http_timeout_seconds: float = Field(default=10.0, gt=0)

    # === Reconciliation and health ===

    #: Job C: how often the books are re-derived from the postings and custody
    #: is compared against the chain. This is the job that would notice an
    #: insolvency, so lengthening it lengthens how long one can go unnoticed.
    reconcile_invariant_interval_seconds: float = Field(default=3600.0, gt=0)
    #: A worker job whose last run is older than this reads as stale in
    #: `/readyz`. `/healthz` is process-and-database only, so this is how a dead
    #: worker is noticed.
    worker_heartbeat_stale_seconds: int = Field(default=300, ge=30)
    #: Failed inbound signature verifications in the last hour before an alert.
    #: A spike means a misconfigured secret or somebody probing the endpoint.
    webhook_signature_failure_threshold: int = Field(default=10, ge=1)
    #: How far custody may sit below user obligations before Job C calls it an
    #: insolvency signal, in the asset's smallest unit. 0 is correct and is the
    #: default: the expected shortfall is derived from in-flight postings, not
    #: from a tuned epsilon.
    custody_tolerance_units: int = Field(default=0, ge=0)

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
        # The Lightning float is a second, separate loss ceiling: channel
        # balance is its own pot of money and the BTC caps say nothing about it.
        if self.seed_ln_withdrawal_auto_limit > self.seed_ln_withdrawal_daily_cap:
            raise ValueError(
                "SEED_LN_WITHDRAWAL_AUTO_LIMIT above SEED_LN_WITHDRAWAL_DAILY_CAP would let a "
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
        # There is no automated TRON signer, so nothing can act on an
        # auto-approved USDT withdrawal. `place_hold` would create the row
        # already in `approved`, and the only code that hands a manual
        # withdrawal to an operator is the admin approve endpoint, whose
        # compare-and-swap requires `pending_approval`. The row would sit
        # there with the user's balance held and no legal action available.
        if self.usdt_auto_withdraw:
            raise ValueError(
                "USDT_AUTO_WITHDRAW=true is not supported: the BTCPay USDt plugin has no "
                "payout handler, so no code can send an auto-approved USDT withdrawal and "
                "the request would stall with the user's balance held. Leave it false "
                "until an automated TRON signer ships."
            )
        if self.platform_webhook_url and not self.platform_webhook_secret:
            raise ValueError(
                "PLATFORM_WEBHOOK_URL is set but PLATFORM_WEBHOOK_SECRET is not; an "
                "unsigned webhook is one anybody can forge"
            )
        if self.deposit_monitoring_minutes < self.deposit_invoice_expiry_min_btc:
            raise ValueError(
                "DEPOSIT_MONITORING_MINUTES below the invoice expiry means BTCPay stops "
                "attributing payments before the invoice is even expired"
            )
        return self

    @property
    def outbound_configured(self) -> bool:
        return bool(self.platform_webhook_url and self.platform_webhook_secret)

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

        Both defaults were confirmed against a live node on 2026-08-11 —
        `symbol()` = USDT and `decimals()` = 6 on each — and the session is
        recorded in docs/operating/verification-log.md. Setting
        USDT_CONTRACT_ADDRESS explicitly still matters: BTCPay's USDt plugin
        watches one token and the withdrawal verifier refuses every other one,
        so the two settings have to name the same contract whatever it is.
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
