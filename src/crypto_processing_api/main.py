"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from crypto_processing_api import __version__
from crypto_processing_api.api import admin, deposits, health, webhooks
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
from crypto_processing_api.services.assets import sync_payment_methods

logger = get_logger(__name__)


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
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info("startup", environment=settings.environment, version=__version__)
    sync_payment_methods_at_startup(settings)
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
        summary="Custodial crypto payment and ledger service",
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(NoStoreMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(deposits.router)
    app.include_router(admin.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
