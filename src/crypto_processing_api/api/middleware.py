"""Middleware and request dependencies: auth, idempotency, no-store, access log."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from crypto_processing_api.config import Settings, get_settings
from crypto_processing_api.core import auth, idempotency
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.db import db_session
from crypto_processing_api.gateway.btcpay_client import BTCPayClient, BTCPayGateway, build_client
from crypto_processing_api.gateway.trongrid import TronGridClient, TronGridGateway
from crypto_processing_api.gateway.trongrid import build_client as build_tron_client

logger = get_logger(__name__)

_gateway: BTCPayClient | None = None
_tron: TronGridClient | None = None


def get_settings_dependency() -> Settings:
    return get_settings()


def get_gateway() -> BTCPayGateway:
    """The one place that names `BTCPayClient`.

    Tests override this dependency with `FakeBTCPay`, so no service module ever
    imports the concrete client.
    """
    global _gateway
    if _gateway is None:
        settings = get_settings()
        _gateway = build_client(
            base_url=settings.btcpay_url,
            api_key=settings.btcpay_api_key,
            store_id=settings.btcpay_store_id,
        )
    return _gateway


def get_tron_gateway() -> TronGridGateway:
    """The one place that names `TronGridClient`. Tests override it."""
    global _tron
    if _tron is None:
        settings = get_settings()
        _tron = build_tron_client(
            base_url=settings.tron_endpoint, api_key=settings.trongrid_api_key
        )
    return _tron


def reset_gateway() -> None:
    global _gateway, _tron
    if _gateway is not None:
        _gateway.close()
    _gateway = None
    if _tron is not None:
        _tron.close()
    _tron = None


_bearer = HTTPBearer(auto_error=False, description="cpk_live_… / cpk_test_… API key")

IDEMPOTENCY_HEADER = "Idempotency-Key"


class NoStoreMiddleware(BaseHTTPMiddleware):
    """Balances and withdrawal states must never be served from a cache.

    Cloudflare sits in front of this API, and a bypass-cache rule is one
    console click away from being lost.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id into the log context and emit one access line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request.failed", duration_ms=round((time.perf_counter() - started) * 1000, 2)
            )
            structlog.contextvars.clear_contextvars()
            raise
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        structlog.contextvars.clear_contextvars()
        return response


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


class RequireApiKey:
    """Dependency factory: `Depends(RequireApiKey(auth.SCOPE_ADMIN))`.

    The client is told only that authentication failed. Which of malformed,
    unknown, revoked or expired it was stays in the log.
    """

    def __init__(self, required_scope: str = auth.SCOPE_READWRITE) -> None:
        self.required_scope = required_scope

    def __call__(
        self,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
        session: Session = Depends(db_session),
    ) -> auth.AuthenticatedKey:
        if credentials is None or not credentials.credentials:
            raise _unauthorized("missing bearer API key")
        try:
            authenticated = auth.authenticate(session, credentials.credentials)
        except auth.AuthError as exc:
            logger.warning("auth.rejected", reason=type(exc).__name__)
            raise _unauthorized("invalid API key") from exc

        try:
            auth.require_scope(authenticated, self.required_scope)
        except auth.InsufficientScope as exc:
            logger.warning(
                "auth.scope_denied", key_id=authenticated.key_id, required=self.required_scope
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="insufficient scope"
            ) from exc

        auth.touch_last_used(session, authenticated.key_id)
        session.commit()
        structlog.contextvars.bind_contextvars(key_id=authenticated.key_id)
        return authenticated


require_readwrite = RequireApiKey(auth.SCOPE_READWRITE)
require_admin = RequireApiKey(auth.SCOPE_ADMIN)


@dataclass(slots=True)
class IdempotencyContext:
    """Handle a mutating endpoint uses to run exactly once.

    A handler starts with `if context.is_replay: return context.replay_response()`
    and finishes with `context.complete(...)` before its final commit.
    """

    session: Session
    key: str
    endpoint: str
    start: idempotency.IdempotentStart | None
    replay: idempotency.ReplayedResponse | None

    @property
    def is_replay(self) -> bool:
        return self.replay is not None

    @property
    def reclaimed(self) -> bool:
        return self.start is not None and self.start.reclaimed

    @property
    def resource_id(self) -> str | None:
        if self.replay is not None:
            return self.replay.resource_id
        return None if self.start is None else self.start.resource_id

    def replay_response(self) -> JSONResponse:
        if self.replay is None:
            raise RuntimeError("replay_response() called on a first-run request")
        return JSONResponse(status_code=self.replay.status, content=self.replay.body)

    def record_resource(self, resource_id: str) -> None:
        idempotency.record_resource(
            self.session, key=self.key, endpoint=self.endpoint, resource_id=resource_id
        )

    def complete(self, *, status_code: int, body: Any, resource_id: str | None = None) -> None:
        idempotency.complete(
            self.session,
            key=self.key,
            endpoint=self.endpoint,
            status=status_code,
            body=body,
            resource_id=resource_id,
        )


class Idempotent:
    """Dependency factory for a mutating endpoint.

    `endpoint` is the idempotency namespace, e.g. "POST /v1/withdrawals": the
    same key on a different endpoint is a different operation.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    async def __call__(
        self,
        request: Request,
        session: Session = Depends(db_session),
    ) -> IdempotencyContext:
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if not key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{IDEMPOTENCY_HEADER} header is required on this endpoint",
            )
        body = await request.body()
        settings = get_settings()
        try:
            outcome = idempotency.begin(
                session,
                key=key,
                endpoint=self.endpoint,
                body=body,
                stale_after_seconds=settings.idempotency_stale_seconds,
            )
        except idempotency.InvalidIdempotencyKey as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except idempotency.RequestMismatch as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{IDEMPOTENCY_HEADER} was already used with a different request body",
            ) from exc
        except idempotency.RequestInFlight as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="a request with this Idempotency-Key is still in progress",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

        if isinstance(outcome, idempotency.ReplayedResponse):
            return IdempotencyContext(
                session=session, key=key, endpoint=self.endpoint, start=None, replay=outcome
            )
        return IdempotencyContext(
            session=session, key=key, endpoint=self.endpoint, start=outcome, replay=None
        )
