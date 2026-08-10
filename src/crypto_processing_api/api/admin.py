"""Admin endpoints: the deposit review queue, the withdrawal queue, wallet alerts."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.api.deposits import serialize_deposit
from crypto_processing_api.api.middleware import get_gateway, require_admin
from crypto_processing_api.api.withdrawals import serialize_withdrawal
from crypto_processing_api.core import auth
from crypto_processing_api.core.amounts import from_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.db import db_session
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway
from crypto_processing_api.ledger.models import WalletTxoAlert, WithdrawalStatus
from crypto_processing_api.services import deposits as deposit_service
from crypto_processing_api.services import withdrawals as withdrawal_service

router = APIRouter(tags=["admin"], prefix="/v1/admin")
logger = get_logger(__name__)


class ResolveDepositRequest(BaseModel):
    """No amount field, deliberately.

    The whole deposit pipeline insists that a webhook payload is never an
    amount source; a human typing one at 2am is not a better source. The admin
    confirms attribution — which deposit, which payment — and the server asks
    BTCPay what that payment was worth.
    """

    action: Literal["credit", "dismiss"]
    payment_id: str | None = None

    @model_validator(mode="after")
    def _credit_needs_payment(self) -> ResolveDepositRequest:
        if self.action == "credit" and not self.payment_id:
            raise ValueError("payment_id is required when action is 'credit'")
        return self


@router.get("/deposits/review", response_model=None)
def review_queue(
    _key: Annotated[auth.AuthenticatedKey, Depends(require_admin)],
    session: Annotated[Session, Depends(db_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = deposit_service.list_review_queue(session, limit=limit)
    items = []
    for deposit in rows:
        asset = deposit_service.get_asset(session, deposit.asset_id, require_enabled=False)
        payments = deposit_service.get_payments(session, deposit.id)
        items.append(serialize_deposit(deposit, decimals=asset.decimals, payments=payments))
    return {"deposits": items}


@router.post("/deposits/{deposit_id}/resolve", response_model=None)
def resolve_deposit(
    deposit_id: uuid.UUID,
    payload: ResolveDepositRequest,
    key: Annotated[auth.AuthenticatedKey, Depends(require_admin)],
    session: Annotated[Session, Depends(db_session)],
    gateway: Annotated[BTCPayGateway, Depends(get_gateway)],
) -> dict[str, Any]:
    try:
        result = deposit_service.resolve_review(
            session,
            gateway,
            deposit_id=deposit_id,
            action=payload.action,
            payment_id=payload.payment_id,
            # The key id, not a free-text name: it is the only identity we can
            # actually verify.
            actor=key.key_id,
        )
    except deposit_service.DepositNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such deposit") from exc
    except deposit_service.PaymentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except deposit_service.IllegalTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except BTCPayError as exc:
        session.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "BTCPay is unreachable; try again"
        ) from exc

    session.commit()
    deposit = deposit_service.get_deposit(session, deposit_id)
    asset = deposit_service.get_asset(session, deposit.asset_id, require_enabled=False)
    payments = deposit_service.get_payments(session, deposit_id)
    return {
        "deposit": serialize_deposit(deposit, decimals=asset.decimals, payments=payments),
        "credited": from_units(result.credited_units, asset.decimals),
    }


@router.get("/wallet-alerts", response_model=None)
def wallet_alerts(
    _key: Annotated[auth.AuthenticatedKey, Depends(require_admin)],
    session: Annotated[Session, Depends(db_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Wallet receives that match no deposit payment.

    These are coins in custody that no user has been credited for — usually a
    payment to an address BTCPay has stopped watching. Job C's aggregate check
    cannot see them, because extra money in the wallet reads as healthy.
    """
    rows = session.execute(
        select(WalletTxoAlert)
        .where(WalletTxoAlert.status == "open")
        .order_by(WalletTxoAlert.detected_at.desc())
        .limit(limit)
    ).scalars()
    items = []
    for alert in rows:
        asset = deposit_service.get_asset(session, alert.asset_id, require_enabled=False)
        items.append(
            {
                "id": alert.id,
                "asset": alert.asset_id,
                "txid": alert.txid,
                "amount": from_units(alert.amount, asset.decimals),
                "confirmations": alert.confirmations,
                "detected_at": alert.detected_at.isoformat(),
                "note": alert.note,
            }
        )
    return {"alerts": items}


class ApproveWithdrawalRequest(BaseModel):
    note: str | None = None


class RejectWithdrawalRequest(BaseModel):
    reason: str | None = None


class ReleaseWithdrawalRequest(BaseModel):
    """A release after submission is an assertion about the chain.

    The operator is stating that they looked and the transaction will not
    arrive — it was never broadcast, or it was double-spent. The text is stored
    on the withdrawal so the decision has an author and a reason.
    """

    attestation: str = Field(min_length=10, max_length=2000)


@router.get("/withdrawals", response_model=None)
def withdrawal_queue(
    _key: Annotated[auth.AuthenticatedKey, Depends(require_admin)],
    session: Annotated[Session, Depends(db_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    parsed: WithdrawalStatus | None = None
    if status_filter is not None:
        try:
            parsed = WithdrawalStatus(status_filter)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown status {status_filter!r}"
            ) from exc

    rows = withdrawal_service.list_by_status(session, status=parsed, limit=limit)
    items = []
    for withdrawal in rows:
        asset = withdrawal_service.get_asset(session, withdrawal.asset_id, require_enabled=False)
        item = serialize_withdrawal(withdrawal, decimals=asset.decimals)
        item["approved_by"] = withdrawal.approved_by
        item["rejected_by"] = withdrawal.rejected_by
        item["released_by"] = withdrawal.released_by
        item["release_attestation"] = withdrawal.release_attestation
        item["backend_ref"] = withdrawal.backend_ref
        items.append(item)
    return {"withdrawals": items}


def _withdrawal_response(session: Session, withdrawal_id: uuid.UUID) -> dict[str, Any]:
    withdrawal = withdrawal_service.get(session, withdrawal_id)
    asset = withdrawal_service.get_asset(session, withdrawal.asset_id, require_enabled=False)
    return serialize_withdrawal(withdrawal, decimals=asset.decimals)


@router.post("/withdrawals/{withdrawal_id}/approve", response_model=None)
def approve_withdrawal(
    withdrawal_id: uuid.UUID,
    _payload: ApproveWithdrawalRequest,
    key: Annotated[auth.AuthenticatedKey, Depends(require_admin)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    try:
        withdrawal_service.approve(session, withdrawal_id, actor=key.key_id)
    except withdrawal_service.WithdrawalNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such withdrawal") from exc
    except withdrawal_service.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return _withdrawal_response(session, withdrawal_id)


@router.post("/withdrawals/{withdrawal_id}/reject", response_model=None)
def reject_withdrawal(
    withdrawal_id: uuid.UUID,
    payload: RejectWithdrawalRequest,
    key: Annotated[auth.AuthenticatedKey, Depends(require_admin)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    try:
        withdrawal_service.reject(session, withdrawal_id, actor=key.key_id, reason=payload.reason)
    except withdrawal_service.WithdrawalNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such withdrawal") from exc
    except withdrawal_service.IllegalTransition as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return _withdrawal_response(session, withdrawal_id)


@router.post("/withdrawals/{withdrawal_id}/release", response_model=None)
def release_withdrawal(
    withdrawal_id: uuid.UUID,
    payload: ReleaseWithdrawalRequest,
    key: Annotated[auth.AuthenticatedKey, Depends(require_admin)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    """Give a hold back after a payout may already exist.

    Never automatic, and never derived from a backend status. The scenario is
    a payout stuck in the mempool: call it failed, refund the user, and then
    watch the original transaction confirm.
    """
    try:
        withdrawal_service.admin_release(
            session, withdrawal_id, actor=key.key_id, attestation=payload.attestation
        )
    except withdrawal_service.WithdrawalNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such withdrawal") from exc
    except withdrawal_service.ReleaseNotPermitted as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except withdrawal_service.WithdrawalError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return _withdrawal_response(session, withdrawal_id)
