"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from crypto_processing_api import __version__
from crypto_processing_api.api import health
from crypto_processing_api.api.middleware import NoStoreMiddleware, RequestContextMiddleware
from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.core.redaction import configure_logging, get_logger
from crypto_processing_api.db import dispose_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info("startup", environment=settings.environment, version=__version__)
    try:
        yield
    finally:
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
    return app


app = create_app()
