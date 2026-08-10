"""Platform-facing withdrawal endpoints."""

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
from crypto_processing_api.gateway.btcpay_client import BTCPayGateway
from crypto_processing_api.ledger.models import Withdrawal
from crypto_processing_api.services import fees
from crypto_processing_api.services import withdrawals as withdrawal_service

router = APIRouter(tags=["withdrawals"])
logger = get_logger(__name__)

ENDPOINT_CREATE = "POST /v1/withdrawals"
MAX_PAGE = 100


class CreateWithdrawalRequest(BaseModel):
    external_user_id: str = Field(min_length=1, max_length=255)
    asset: str = Field(min_length=1, max_length=32)
    amount: str = Field(description="Gross amount in integer smallest units, as a string")
    destination_address: str = Field(min_length=1, max_length=255)

    @field_validator("amount")
    @classmethod
    def _positive_integer_string(cls, value: str) -> str:
        if not value.isdigit() or int(value) <= 0:
            raise ValueError("amount must be a positive integer as a string")
        return value


def serialize_withdrawal(withdrawal: Withdrawal, *, decimals: int) -> dict[str, Any]:
    return {
        "withdrawal_id": str(withdrawal.id),
        "external_user_id": withdrawal.external_user_id,
        "asset": withdrawal.asset_id,
        "status": withdrawal.status.value,
        "destination_address": withdrawal.destination_address,
        "amount_gross": from_units(withdrawal.amount_gross, decimals),
        "fee": (
            from_units(withdrawal.fee_amount, decimals)
            if withdrawal.fee_amount is not None
            else None
        ),
        "amount_net": (
            from_units(withdrawal.amount_net, decimals)
            if withdrawal.amount_net is not None
            else None
        ),
        "approval_mode": withdrawal.approval_mode,
        "txid": withdrawal.txid,
        "failure_reason": withdrawal.failure_reason,
        "created_at": withdrawal.created_at.isoformat(),
        "updated_at": withdrawal.updated_at.isoformat(),
    }


@router.post("/v1/withdrawals", status_code=status.HTTP_201_CREATED, response_model=None)
def create_withdrawal(
    payload: CreateWithdrawalRequest,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
    gateway: Annotated[BTCPayGateway, Depends(get_gateway)],
    settings: Annotated[Settings, Depends(get_settings_dependency)],
    context: Annotated[IdempotencyContext, Depends(Idempotent(ENDPOINT_CREATE))],
) -> JSONResponse:
    if context.is_replay:
        return context.replay_response()

    gross = int(payload.amount)
    try:
        asset = withdrawal_service.get_asset(session, payload.asset)
    except withdrawal_service.WithdrawalError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    # Everything that can be refused without touching money is refused first,
    # so a doomed request never reserves the user's balance.
    try:
        withdrawal_service.validate_destination(
            session, settings, asset_id=asset.id, address=payload.destination_address
        )
    except withdrawal_service.InvalidDestination as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    try:
        # A quote at request time, for the dust check only. The fee that gets
        # charged is quoted again at submission, because that is when it is
        # fixed.
        fees.quote_btc_fee(
            gateway, settings, gross=gross, payment_method_id=asset.btcpay_payment_method
        )
    except fees.DustAmount as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    try:
        outcome = withdrawal_service.place_hold(
            session,
            external_user_id=payload.external_user_id,
            asset_id=asset.id,
            amount_gross=gross,
            destination_address=payload.destination_address,
        )
    except withdrawal_service.AmountTooSmall as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except withdrawal_service.InsufficientBalance as exc:
        session.rollback()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
    except withdrawal_service.WithdrawalError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    body = serialize_withdrawal(outcome.withdrawal, decimals=asset.decimals)
    if outcome.reason:
        body["approval_reason"] = outcome.reason
    context.record_resource(str(outcome.withdrawal.id))
    context.complete(status_code=status.HTTP_201_CREATED, body=body)
    # The hold, the withdrawal row and the idempotency record commit together.
    session.commit()
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=body)


@router.get("/v1/withdrawals/{withdrawal_id}", response_model=None)
def get_withdrawal(
    withdrawal_id: uuid.UUID,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    try:
        withdrawal = withdrawal_service.get(session, withdrawal_id)
    except withdrawal_service.WithdrawalNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such withdrawal") from exc
    asset = withdrawal_service.get_asset(session, withdrawal.asset_id, require_enabled=False)
    return serialize_withdrawal(withdrawal, decimals=asset.decimals)


@router.get("/v1/users/{external_user_id}/withdrawals", response_model=None)
def list_user_withdrawals(
    external_user_id: str,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 25,
    cursor: uuid.UUID | None = None,
) -> dict[str, Any]:
    rows = withdrawal_service.list_for_user(
        session, external_user_id=external_user_id, limit=limit, cursor=cursor
    )
    decimals = {
        asset_id: withdrawal_service.get_asset(session, asset_id, require_enabled=False).decimals
        for asset_id in {row.asset_id for row in rows}
    }
    return {
        "withdrawals": [serialize_withdrawal(row, decimals=decimals[row.asset_id]) for row in rows],
        "next_cursor": str(rows[-1].id) if len(rows) == limit else None,
    }
