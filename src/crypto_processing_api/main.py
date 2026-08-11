"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from crypto_processing_api import __version__
from crypto_processing_api.api import admin, balances, deposits, health, webhooks, withdrawals
from crypto_processing_api.api.middleware import (
    NoStoreMiddleware,
    RequestContextMiddleware,
    get_gateway,
    reset_gateway,
)
from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.core.redaction import configure_logging, get_logger
from crypto_processing_api.db import dispose_engine, session_scope
from crypto_processing_api.gateway.btcpay_client import BTCPayError
from crypto_processing_api.services.asset_registry import (
    assert_every_enabled_asset_has_a_profile,
    get_registry,
)
from crypto_processing_api.services.assets import sync_payment_methods

logger = get_logger(__name__)

DESCRIPTION = """\
The accounting layer BTCPay Server does not have: per-user custodial balances,
deposits and withdrawals over a small authenticated JSON API, backed by an
append-only double-entry ledger.

**Amounts are decimal strings, always** — `"0.50000000"`, `"199.000000"` —
never JSON numbers, because 21 million BTC in satoshis is past JavaScript's
safe integer range. The two request fields `expected_amount` and `amount` are
integer strings of the asset's smallest unit (`"50000000"`).

**Timestamps are ISO 8601 with an explicit `+00:00` offset.**

**Auth** is `Authorization: Bearer cpk_live_…`. Two scopes: `readwrite` for
platform calls, `admin` for operator calls. An admin key also satisfies
`readwrite`.

**Every mutating endpoint requires an `Idempotency-Key` header**, and a retry
must reuse the same key. A new key is a second deposit or a second withdrawal.

**Errors** are `{"detail": "..."}`. The one exception is a pooled-address
deposit failure, which uses a structured `detail` object carrying a `code`.

**Every response carries `Cache-Control: no-store`.**

The outbound webhook payloads this service sends are documented separately, in
`docs/reference/webhook-events.json`.
"""

TAGS_METADATA = [
    {"name": "deposits", "description": "Create a deposit and follow it to `settled`."},
    {"name": "withdrawals", "description": "Request a withdrawal. The hold is placed with it."},
    {"name": "balances", "description": "Balances, ledger history and asset reference data."},
    {
        "name": "admin",
        "description": "The review and approval queues, the outbound queue, and the "
        "custody report. Needs an `admin` key.",
    },
    {"name": "health", "description": "Liveness and component readiness. No key needed."},
    {
        "name": "webhooks",
        "description": "BTCPay's ingress. Authenticated by HMAC over the raw body, "
        "never by an API key.",
    },
]


def sync_payment_methods_at_startup(settings: Settings) -> None:
    """Resolve BTCPay's payment-method ids into the `assets` rows.

    Two failure modes, deliberately treated differently:

    - **A missing payment method for a required asset raises**, and the process
      does not start. Creating invoices for a method the store does not serve
      produces deposit addresses nobody watches.
    - **BTCPay being unreachable only logs.** That is an outage, not a
      misconfiguration, and balances and deposit reads are still perfectly
      serviceable. The worker retries on its own schedule.
    """
    if not settings.btcpay_configured:
        logger.warning(
            "startup.btcpay_unconfigured",
            detail="deposit endpoints will fail until BTCPAY_URL, BTCPAY_API_KEY "
            "and BTCPAY_STORE_ID are set",
        )
        return
    try:
        with session_scope() as session:
            report = sync_payment_methods(session, get_gateway())
    except BTCPayError as exc:
        logger.error("startup.payment_method_sync_failed", error=str(exc))
        return
    logger.info(
        "startup.payment_methods",
        resolved=report.resolved,
        updated=report.updated,
        disabled=report.disabled,
        enabled=report.enabled,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info("startup", environment=settings.environment, version=__version__)
    sync_payment_methods_at_startup(settings)
    # After the sync, not before: the sync is what disables an asset the store
    # cannot serve, and a disabled asset needs no profile.
    with session_scope() as session:
        assert_every_enabled_asset_has_a_profile(session, get_registry())
    try:
        yield
    finally:
        reset_gateway()
        dispose_engine()
        logger.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)

    app = FastAPI(
        title="crypto-processing-api",
        version=__version__,
        summary="Custodial crypto payment and ledger service on top of BTCPay Server",
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        license_info={"name": "MIT", "identifier": "MIT"},
        contact={
            "name": "crypto-processing-api",
            "url": "https://github.com/OliverD25/crypto-processing-api",
        },
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(deposits.router)
    app.include_router(withdrawals.router)
    app.include_router(balances.router)
    app.include_router(admin.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
