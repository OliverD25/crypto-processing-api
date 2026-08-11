"""Balance, ledger-history and asset-reference reads.

Balances come from the materialized `accounts.balance`, which is updated inside
the same transaction as the postings that changed it. Summing postings on every
read would be correct too, and slower; the reconciliation job is what proves
the two still agree.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from crypto_processing_api.api import schemas
from crypto_processing_api.api.middleware import require_readwrite
from crypto_processing_api.core import auth
from crypto_processing_api.core.amounts import from_units
from crypto_processing_api.db import db_session
from crypto_processing_api.ledger.models import (
    Account,
    AccountKind,
    Asset,
    Deposit,
    JournalEntry,
    Posting,
)

router = APIRouter(tags=["balances"])

MAX_PAGE = 200


@router.get(
    "/v1/users/{external_user_id}/balances",
    response_model=schemas.BalancesResponse,
    operation_id="getUserBalances",
    summary="Per-asset available, held and total",
    responses=schemas.error_responses(401, 403),
)
def user_balances(
    external_user_id: str,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    """Per-asset available, held and total.

    User accounts are credit-normal, so their stored balance is negative and
    the sign is flipped here. `held` is not arithmetic on a side table: a hold
    physically moved the money between two accounts, so there is nothing to get
    wrong at read time.
    """
    rows = session.execute(
        select(Account).where(
            Account.external_user_id == external_user_id,
            Account.kind.in_((AccountKind.USER_AVAILABLE, AccountKind.USER_HOLD)),
        )
    ).scalars()

    totals: dict[str, dict[str, int]] = {}
    for account in rows:
        entry = totals.setdefault(account.asset_id, {"available": 0, "held": 0})
        key = "available" if account.kind == AccountKind.USER_AVAILABLE else "held"
        entry[key] = -account.balance

    assets = {
        asset.id: asset
        for asset in session.execute(select(Asset).where(Asset.id.in_(totals.keys()))).scalars()
    }

    balances = []
    for asset_id, amounts in sorted(totals.items()):
        decimals = assets[asset_id].decimals if asset_id in assets else 8
        balances.append(
            {
                "asset": asset_id,
                "available": from_units(amounts["available"], decimals),
                "held": from_units(amounts["held"], decimals),
                "total": from_units(amounts["available"] + amounts["held"], decimals),
            }
        )
    return {"external_user_id": external_user_id, "balances": balances}


@router.get(
    "/v1/users/{external_user_id}/transactions",
    response_model=schemas.TransactionsResponse,
    operation_id="getUserTransactions",
    summary="Ledger history from the user's point of view",
    responses=schemas.error_responses(401, 403),
)
def user_transactions(
    external_user_id: str,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
    asset: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
    cursor: int | None = None,
) -> dict[str, Any]:
    """Ledger history from the user's point of view.

    Amounts are negated relative to the stored posting, so a deposit reads as
    positive and a withdrawal hold as negative — the user's perspective, not
    the book's.
    """
    query = (
        select(Posting, JournalEntry, Account)
        .join(JournalEntry, JournalEntry.id == Posting.entry_id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Account.external_user_id == external_user_id,
            Account.kind.in_((AccountKind.USER_AVAILABLE, AccountKind.USER_HOLD)),
        )
    )
    if asset is not None:
        query = query.where(Account.asset_id == asset)
    if cursor is not None:
        query = query.where(Posting.id < cursor)

    rows = session.execute(query.order_by(Posting.id.desc()).limit(limit)).all()
    decimals = {item.id: item.decimals for item in session.execute(select(Asset)).scalars()}

    transactions = [
        {
            "posting_id": posting.id,
            "entry_id": entry.id,
            "kind": entry.kind.value,
            "asset": account.asset_id,
            "account": account.kind.value,
            "amount": from_units(abs(posting.amount), decimals.get(account.asset_id, 8)),
            "direction": "credit" if posting.amount < 0 else "debit",
            "source_ref": entry.source_ref,
            "memo": entry.memo,
            "created_at": posting.created_at.isoformat(),
        }
        for posting, entry, account in rows
    ]
    return {
        "transactions": transactions,
        "next_cursor": rows[-1][0].id if len(rows) == limit else None,
    }


@router.get(
    "/v1/assets",
    response_model=schemas.AssetsResponse,
    operation_id="listAssets",
    summary="Enabled assets, their decimals and their limits",
    responses=schemas.error_responses(401, 403),
)
def list_assets(
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    """Reference data for integrators: what is enabled and what the limits are."""
    rows = session.execute(select(Asset).order_by(Asset.id)).scalars()
    return {
        "assets": [
            {
                "asset": asset.id,
                "display_name": asset.display_name,
                "decimals": asset.decimals,
                "unit_name": asset.unit_name,
                "enabled": asset.enabled,
                "withdrawal_min": from_units(asset.withdrawal_min, asset.decimals),
                "withdrawal_auto_limit": from_units(asset.withdrawal_auto_limit, asset.decimals),
                "withdrawal_daily_cap": from_units(asset.withdrawal_daily_cap, asset.decimals),
                "withdrawal_flat_fee": from_units(asset.withdrawal_flat_fee, asset.decimals),
            }
            for asset in rows
        ]
    }


@router.get(
    "/v1/deposits/{deposit_id}/address-history",
    response_model=schemas.AddressHistoryResponse,
    operation_id="getAddressHistory",
    summary="Who held this deposit's address, and when",
    responses=schemas.error_responses(401, 403, 404),
)
def address_history(
    deposit_id: uuid.UUID,
    _key: Annotated[auth.AuthenticatedKey, Depends(require_readwrite)],
    session: Annotated[Session, Depends(db_session)],
) -> dict[str, Any]:
    """Every deposit that has held this deposit's address, and when.

    Only interesting for pooled assets. It is the query the USDT attribution
    runbook runs: given a transfer that arrived at time T, which user actually
    owned the address then?
    """
    deposit = session.get(Deposit, deposit_id)
    if deposit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such deposit")
    if not deposit.address:
        return {"address": None, "reservations": []}

    rows = session.execute(
        select(Deposit)
        .where(Deposit.asset_id == deposit.asset_id, Deposit.address == deposit.address)
        .order_by(Deposit.created_at.desc())
        .limit(100)
    ).scalars()
    return {
        "address": deposit.address,
        "reservations": [
            {
                "deposit_id": str(row.id),
                "external_user_id": row.external_user_id,
                "status": row.status.value,
                "reserved_from": (
                    row.address_reserved_from.isoformat() if row.address_reserved_from else None
                ),
                "reserved_until": (
                    row.address_reserved_until.isoformat() if row.address_reserved_until else None
                ),
            }
            for row in rows
        ],
    }
