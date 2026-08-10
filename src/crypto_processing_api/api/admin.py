"""Admin endpoints: the deposit review queue and unattributed wallet receives."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.api.deposits import serialize_deposit
from crypto_processing_api.api.middleware import get_gateway, require_admin
from crypto_processing_api.core import auth
from crypto_processing_api.core.amounts import from_units
from crypto_processing_api.core.redaction import get_logger
from crypto_processing_api.db import db_session
from crypto_processing_api.gateway.btcpay_client import BTCPayError, BTCPayGateway
from crypto_processing_api.ledger.models import WalletTxoAlert
from crypto_processing_api.services import deposits as deposit_service

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
