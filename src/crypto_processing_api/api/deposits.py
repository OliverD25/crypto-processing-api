"""Platform-facing deposit endpoints.

Amounts cross this boundary as decimal strings of integer smallest units
("50000000" satoshis), never as JSON numbers: a JavaScript client silently
truncates integers above 2^53, and 21 million BTC in satoshis is well past
that.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from crypto_processing_api.api.middleware import (
    IdempotencyContext,
    Idempotent,
    get_gateway,
    get_settings_dependency,
    require_readwrite,
)
from crypto_processing_api.config import Settings
from crypto_processing_api.core import auth
from crypto_processing_api.core.amounts import from_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.db import db_session
from crypto_processing_api.gateway.btcpay_client import (
    BTCPayAuthError,
    BTCPayError,
    BTCPayGateway,
    BTCPayNotFound,
    BTCPayValidation,
)
from crypto_processing_api.ledger.models import Deposit, DepositPayment
from crypto_processing_api.services import deposits as deposit_service

router = APIRouter(tags=["deposits"])
logger = get_logger(__name__)

ENDPOINT_CREATE = "POST /v1/deposits"
MAX_PAGE = 100


class CreateDepositRequest(BaseModel):
    external_user_id: str = Field(min_length=1, max_length=255)
    asset: str = Field(min_length=1, max_length=32)
    expected_amount: str | None = Field(
        default=None,
        description="Display only, integer smallest units as a string. Top-up invoices "
        "credit whatever arrives.",
    )

    @field_validator("expected_amount")
    @classmethod
    def _positive_integer_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("expected_amount must be a positive integer as a string")
        return value


def serialize_payment(payment: DepositPayment, decimals: int) -> dict[str, Any]:
    return {
        "payment_id": payment.btcpay_payment_id,
        "amount": from_units(payment.amount, decimals),
        "credited": payment.credited_at is not None,
        "credited_at": payment.credited_at.isoformat() if payment.credited_at else None,
        "after_expiration": payment.after_expiration,
        "resolved_by": payment.resolved_by,
    }


def serialize_deposit(
    deposit: Deposit, *, decimals: int, payments: list[DepositPayment] | None = None
) -> dict[str, Any]:
    return {
        "deposit_id": str(deposit.id),
        "external_user_id": deposit.external_user_id,
        "asset": deposit.asset_id,
        "status": deposit.status.value,
        "address": deposit.address,
        "checkout_link": deposit.checkout_link,
        "expires_at": deposit.expires_at.isoformat() if deposit.expires_at else None,
        "address_reserved_until": (
            deposit.address_reserved_until.isoformat() if deposit.address_reserved_until else None
        ),
        "amount_expected": (
            from_units(deposit.amount_expected, decimals)
            if deposit.amount_expected is not None
            else None
        ),
        "amount_credited": from_units(deposit.amount_credited, decimals),
        "created_at": deposit.created_at.isoformat(),
        "payments": [serialize_payment(p, decimals) for p in (payments or [])],
    }


@router.post("/v1/deposits", status_code=status.HTTP_201_CREATED, response_model=None)
def create_deposit(
    payload: CreateDepositRequest,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
    gateway: Annotated[BTCPayGateway, Depends(get_gateway)],
    settings: Annotated[Settings, Depends(get_settings_dependency)],
    context: Annotated[IdempotencyContext, Depends(Idempotent(ENDPOINT_CREATE))],
) -> JSONResponse:
    if context.is_replay:
        return context.replay_response()

    try:
        asset = deposit_service.get_asset(session, payload.asset)
    except deposit_service.UnknownAsset as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown asset {payload.asset}") from exc
    except deposit_service.AssetUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"{payload.asset} deposits are unavailable"
        ) from exc

    deposit: Deposit | None = None
    if context.reclaimed and context.resource_id:
        # A previous attempt died somewhere after committing the row. Pick it
        # back up rather than creating a second deposit for one request.
        deposit = session.get(Deposit, uuid.UUID(context.resource_id))
    if deposit is None:
        deposit = deposit_service.create_deposit(
            session,
            external_user_id=payload.external_user_id,
            asset_id=asset.id,
            amount_expected=int(payload.expected_amount) if payload.expected_amount else None,
        )
        context.record_resource(str(deposit.id))
    # The intent and its idempotency claim become durable together, before
    # anything leaves this process.
    session.commit()

    try:
        deposit_service.ensure_invoice(
            session, gateway, settings, deposit=deposit, adopt_first=context.reclaimed
        )
    except BTCPayValidation as exc:
        deposit_service.mark_failed(session, deposit=deposit, reason=str(exc))
        session.commit()
        if asset.id in deposit_service.POOLED_ASSETS:
            # The USDt plugin hands out addresses from a fixed pool, so a
            # validation failure here usually means every address is reserved.
            # That is temporary and the platform should retry, which a 502
            # would not tell it.
            logger.error("deposit.pool_possibly_exhausted", asset=asset.id, error=str(exc))
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                {
                    "code": "DEPOSIT_TEMPORARILY_UNAVAILABLE",
                    "message": (f"no {asset.id} deposit address is free right now; retry shortly"),
                },
                headers={"Retry-After": "60"},
            ) from exc
        logger.error("deposit.invoice_rejected", deposit_id=str(deposit.id), error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "BTCPay rejected the invoice request"
        ) from exc
    except (BTCPayAuthError, BTCPayNotFound) as exc:
        # BTCPay answered and said no. That is definite, so the intent is dead.
        deposit_service.mark_failed(session, deposit=deposit, reason=str(exc))
        session.commit()
        logger.error("deposit.invoice_rejected", deposit_id=str(deposit.id), error=str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "BTCPay rejected the invoice request"
        ) from exc
    except BTCPayError as exc:
        # Ambiguous. The invoice may exist. Leave the row in `creating`, leave
        # the idempotency record in_progress, and let either a client retry
        # after the staleness window or the reconciliation sweep adopt it.
        session.rollback()
        logger.warning("deposit.invoice_ambiguous", deposit_id=str(deposit.id), error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "BTCPay is unreachable; retry with the same Idempotency-Key",
            headers={"Retry-After": "60"},
        ) from exc

    body = serialize_deposit(deposit, decimals=asset.decimals, payments=[])
    context.complete(status_code=status.HTTP_201_CREATED, body=body)
    session.commit()
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=body)


@router.get("/v1/deposits/{deposit_id}", response_model=None)
def get_deposit(
    deposit_id: uuid.UUID,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    try:
        deposit = deposit_service.get_deposit(session, deposit_id)
    except deposit_service.DepositNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such deposit") from exc
    asset = deposit_service.get_asset(session, deposit.asset_id, require_enabled=False)
    payments = deposit_service.get_payments(session, deposit.id)
    return serialize_deposit(deposit, decimals=asset.decimals, payments=payments)


@router.get("/v1/users/{external_user_id}/deposits", response_model=None)
def list_user_deposits(
    external_user_id: str,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 25,
    cursor: uuid.UUID | None = None,
) -> dict[str, Any]:
    rows = deposit_service.list_user_deposits(
        session, external_user_id=external_user_id, limit=limit, cursor=cursor
    )
    decimals = {
        asset_id: deposit_service.get_asset(session, asset_id, require_enabled=False).decimals
        for asset_id in {row.asset_id for row in rows}
    }
    return {
        "deposits": [
            serialize_deposit(row, decimals=decimals[row.asset_id], payments=[]) for row in rows
        ],
        # Keyset, not offset: a page boundary cannot shift under an insert.
        "next_cursor": str(rows[-1].id) if len(rows) == limit else None,
    }
