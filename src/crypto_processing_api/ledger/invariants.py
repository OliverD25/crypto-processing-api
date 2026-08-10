"""Derived-vs-materialized checks and the custody identity.

Used by the integration test fixture after every scenario now, and by
reconciliation Job C hourly later. These functions only report — an invariant
failure is a severity-1 bug, and a job that silently "fixes" the books would
destroy the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crypto_processing_api.ledger.models import Account, AccountKind, JournalEntry, Posting


class InvariantViolation(Exception):
    """A ledger invariant does not hold."""


@dataclass(frozen=True, slots=True)
class BalanceDrift:
    account_id: int
    asset_id: str
    kind: AccountKind
    external_user_id: str | None
    materialized: int
    derived: int

    @property
    def difference(self) -> int:
        return self.materialized - self.derived


@dataclass(frozen=True, slots=True)
class EntryImbalance:
    entry_id: int
    total: int


@dataclass(frozen=True, slots=True)
class CustodyReport:
    """Per-asset view of the books, in human-facing signs.

    `custody` is what the ledger believes is under our control; the user
    figures are positive when we owe them. `residual` is the sum of the raw
    signed balances and must be exactly 0 — anything else means a posting
    reached the database without going through `post_entry`.
    """

    asset_id: str
    custody: int
    user_available: int
    user_hold: int
    fee_income: int
    network_fee_expense: int
    external: int
    user_deficit: int
    residual: int

    @property
    def user_obligations(self) -> int:
        return self.user_available + self.user_hold

    @property
    def balanced(self) -> bool:
        return self.residual == 0


def derived_balances(session: Session) -> dict[int, int]:
    """`SUM(postings.amount)` per account, including accounts with no postings."""
    rows = session.execute(
        select(Account.id, func.coalesce(func.sum(Posting.amount), 0))
        .select_from(Account)
        .outerjoin(Posting, Posting.account_id == Account.id)
        .group_by(Account.id)
    ).all()
    return {int(account_id): int(total) for account_id, total in rows}


def balance_drifts(session: Session) -> list[BalanceDrift]:
    derived = derived_balances(session)
    accounts = session.execute(select(Account)).scalars().all()
    return [
        BalanceDrift(
            account_id=account.id,
            asset_id=account.asset_id,
            kind=account.kind,
            external_user_id=account.external_user_id,
            materialized=account.balance,
            derived=derived.get(account.id, 0),
        )
        for account in accounts
        if account.balance != derived.get(account.id, 0)
    ]


def unbalanced_entries(session: Session) -> list[EntryImbalance]:
    rows = session.execute(
        select(JournalEntry.id, func.coalesce(func.sum(Posting.amount), 0))
        .select_from(JournalEntry)
        .outerjoin(Posting, Posting.entry_id == JournalEntry.id)
        .group_by(JournalEntry.id)
        .having(func.coalesce(func.sum(Posting.amount), 0) != 0)
    ).all()
    return [EntryImbalance(entry_id=int(entry_id), total=int(total)) for entry_id, total in rows]


def custody_reports(session: Session) -> list[CustodyReport]:
    rows = session.execute(
        select(
            Account.asset_id, Account.kind, func.coalesce(func.sum(Account.balance), 0)
        ).group_by(Account.asset_id, Account.kind)
    ).all()

    totals: dict[str, dict[AccountKind, int]] = {}
    for asset_id, kind, balance in rows:
        totals.setdefault(str(asset_id), {})[AccountKind(kind)] = int(balance)

    reports: list[CustodyReport] = []
    for asset_id, by_kind in sorted(totals.items()):

        def raw(kind: AccountKind, table: dict[AccountKind, int] = by_kind) -> int:
            return table.get(kind, 0)

        reports.append(
            CustodyReport(
                asset_id=asset_id,
                custody=raw(AccountKind.HOT_WALLET) + raw(AccountKind.PAYOUTS_IN_FLIGHT),
                user_available=-raw(AccountKind.USER_AVAILABLE),
                user_hold=-raw(AccountKind.USER_HOLD),
                fee_income=-raw(AccountKind.FEE_INCOME),
                network_fee_expense=raw(AccountKind.NETWORK_FEE_EXPENSE),
                external=-raw(AccountKind.EXTERNAL),
                user_deficit=raw(AccountKind.USER_DEFICIT),
                residual=sum(by_kind.values()),
            )
        )
    return reports


def assert_ledger_consistent(session: Session) -> None:
    """Raise `InvariantViolation` if the books do not hold together.

    Checks, in order: every entry sums to zero, every materialized balance
    equals its derived sum, and every asset's signed balances sum to zero.
    """
    imbalances = unbalanced_entries(session)
    if imbalances:
        detail = ", ".join(f"entry {i.entry_id} sums to {i.total}" for i in imbalances)
        raise InvariantViolation(f"unbalanced journal entries: {detail}")

    drifts = balance_drifts(session)
    if drifts:
        detail = ", ".join(
            f"account {d.account_id} materialized={d.materialized} derived={d.derived}"
            for d in drifts
        )
        raise InvariantViolation(f"materialized balances drifted from postings: {detail}")

    unbalanced_assets = [r for r in custody_reports(session) if not r.balanced]
    if unbalanced_assets:
        detail = ", ".join(f"{r.asset_id} residual={r.residual}" for r in unbalanced_assets)
        raise InvariantViolation(f"asset books do not sum to zero: {detail}")
