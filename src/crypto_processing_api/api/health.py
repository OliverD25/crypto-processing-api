"""Liveness and readiness.

`/healthz` reports the process and its database, and nothing else. Coupling it
to BTCPay would mean a routine BTCPay upgrade pages the operator and, if
compose healthchecks gate on it, restarts an API whose balances and ledger
reads are perfectly fine. Component reachability (BTCPay, TronGrid) belongs on
`/readyz`, which arrives with the reconciliation work in M5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from crypto_processing_api import __version__
from crypto_processing_api.api import schemas
from crypto_processing_api.api.middleware import (
    get_gateway,
    get_settings_dependency,
    get_tron_gateway,
)
from crypto_processing_api.config import Settings
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.db import db_session
from crypto_processing_api.ledger.models import WorkerHeartbeat

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get(
    "/healthz",
    summary="Process and database liveness",
    response_model=schemas.HealthResponse,
    operation_id="healthz",
    responses={
        503: {
            "model": schemas.HealthResponse,
            "description": "The database is unreachable. Nothing else is checked here.",
        }
    },
)
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


def _component(name: str, ok: bool, detail: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "status": "ok" if ok else "degraded"}
    if detail:
        entry["detail"] = detail
    return entry


@router.get(
    "/readyz",
    summary="Component readiness",
    response_model=schemas.ReadyResponse,
    operation_id="readyz",
    responses={
        503: {
            "model": schemas.ReadyResponse,
            "description": "At least one component is degraded. The body names which.",
        }
    },
)
def readyz(
    session: Annotated[Session, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings_dependency)],
) -> JSONResponse:
    """Everything `/healthz` deliberately leaves out.

    BTCPay restarts are routine on a shared box, and TronGrid is a third party
    on a free tier. Neither should be able to make this service look down, so
    they are reported here instead, where an operator can look without an
    uptime pinger restarting the API over it.
    """
    components: list[dict[str, Any]] = []

    try:
        session.execute(text("SELECT 1"))
        components.append(_component("database", True))
    except SQLAlchemyError as exc:
        components.append(_component("database", False, type(exc).__name__))

    if settings.btcpay_configured:
        try:
            get_gateway().get_store_payment_methods(only_enabled=True)
            components.append(_component("btcpay", True))
        except Exception as exc:
            components.append(_component("btcpay", False, type(exc).__name__))
    else:
        components.append(_component("btcpay", False, "not configured"))

    if settings.tron_configured:
        try:
            get_tron_gateway().get_block_height()
            components.append(_component("trongrid", True))
        except Exception as exc:
            components.append(_component("trongrid", False, type(exc).__name__))

    components.extend(_worker_components(session, settings))

    ready = all(entry["status"] == "ok" for entry in components)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "degraded", "components": components},
    )


def _worker_components(session: Session, settings: Settings) -> list[dict[str, Any]]:
    """Heartbeat staleness per job.

    A worker that stopped is invisible from every read endpoint: balances still
    answer, deposits still create, and nothing is being credited.
    """
    try:
        rows = list(session.execute(select(WorkerHeartbeat)).scalars())
    except SQLAlchemyError as exc:
        return [_component("worker", False, type(exc).__name__)]

    if not rows:
        return [_component("worker", False, "no job has reported yet")]

    now = datetime.now(UTC)
    stale = [
        f"{row.job_name} last ran {int((now - row.last_run_at).total_seconds())}s ago"
        for row in rows
        if (now - row.last_run_at).total_seconds() > settings.worker_heartbeat_stale_seconds
    ]
    if stale:
        return [_component("worker", False, "; ".join(sorted(stale))[:400])]
    return [_component("worker", True, f"{len(rows)} jobs reporting")]
