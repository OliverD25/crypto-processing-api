"""Liveness endpoint.

`/healthz` reports the process and its database, and nothing else. Coupling it
to BTCPay would mean a routine BTCPay upgrade pages the operator and, if
compose healthchecks gate on it, restarts an API whose balances and ledger
reads are perfectly fine. Component reachability (BTCPay, TronGrid) belongs on
`/readyz`, which arrives with the reconciliation work in M5.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from crypto_processing_api import __version__
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.db import db_session

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/healthz", summary="Process and database liveness")
def healthz(session: Session = Depends(db_session)) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error("healthz.database_unreachable", error=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unreachable", "version": __version__},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "database": "ok", "version": __version__},
    )
