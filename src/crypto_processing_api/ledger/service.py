"""The only module allowed to write `postings`.

Everything that moves money calls `post_entry`. It locks the affected account
rows in ascending id order, refuses to write an entry that does not sum to
zero, and updates the materialized balances inside the same transaction so the
`no_overdraft` CHECK can act as the database-level last line of defence.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from crypto_processing_api.ledger.models import (
    NORMAL_SIDE_BY_KIND,
    USER_ACCOUNT_KINDS,
    Account,
    AccountKind,
    Asset,
    EntryKind,
    JournalEntry,
    Posting,
)

PostingPair = tuple[int, int]


class LedgerError(Exception):
    """Base class for every typed failure the ledger raises."""


class UnbalancedEntry(LedgerError):
    """The postings do not sum to zero, or there are too few of them."""


class ZeroPosting(LedgerError):
    """A posting with amount 0 carries no information and is rejected."""


class AccountNotFound(LedgerError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)


class AssetMismatch(LedgerError):
    """A posting targets an account belonging to a different asset."""


class AlreadyPosted(LedgerError):
    """`(kind, source_ref)` already exists — the effect happened before.

    Callers treat this as success: it is what a replayed webhook, a poller
    racing that webhook, or a client retry all look like at the ledger.
    """

    def __init__(self, kind: EntryKind, source_ref: str, entry: JournalEntry | None) -> None:
        super().__init__(f"entry {kind.value}/{source_ref} already posted")
        self.kind = kind
        self.source_ref = source_ref
        self.entry = entry

    @property
    def entry_id(self) -> int | None:
        return None if self.entry is None else self.entry.id


class InsufficientFunds(LedgerError):
    """A balance CHECK rejected the entry (overdraft or negative custody)."""

    def __init__(self, constraint: str) -> None:
        super().__init__(f"balance constraint {constraint} rejected the entry")
        self.constraint = constraint


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """Seed values for one `assets` row.

    Read from the environment exactly once. After the row exists the database
    is the only source of truth for limits and fees.
    """

    id: str
    display_name: str
    decimals: int
    unit_name: str
    btcpay_payment_method: str
    withdrawal_auto_limit: int
    withdrawal_daily_cap: int
    withdrawal_user_daily_cap: int | None
    withdrawal_min: int
    withdrawal_flat_fee: int


SYSTEM_ACCOUNT_KINDS: tuple[AccountKind, ...] = (
    AccountKind.HOT_WALLET,
    AccountKind.PAYOUTS_IN_FLIGHT,
    AccountKind.FEE_INCOME,
    AccountKind.NETWORK_FEE_EXPENSE,
    AccountKind.EXTERNAL,
    AccountKind.USER_DEFICIT,
)


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None


def get_or_create_account(
    session: Session,
    *,
    asset_id: str,
    kind: AccountKind,
    external_user_id: str | None = None,
) -> Account:
    """Provision an account row race-safely.

    Two concurrent first-ever deposits for the same user must not deadlock or
    duplicate, so creation goes through ON CONFLICT DO NOTHING against the
    partial unique index rather than a check-then-insert.
    """
    is_user_kind = kind in USER_ACCOUNT_KINDS
    if is_user_kind and external_user_id is None:
        raise AccountNotFound(f"{kind.value} accounts require an external_user_id")
    if not is_user_kind and external_user_id is not None:
        raise AccountNotFound(f"{kind.value} is a system account and takes no external_user_id")

    stmt = (
        pg_insert(Account)
        .values(
            asset_id=asset_id,
            kind=kind.value,
            external_user_id=external_user_id,
            normal_side=NORMAL_SIDE_BY_KIND[kind].value,
            balance=0,
        )
        .on_conflict_do_nothing(
            index_elements=["asset_id", "kind", "external_user_id"]
            if is_user_kind
            else ["asset_id", "kind"],
            index_where=Account.external_user_id.isnot(None)
            if is_user_kind
            else Account.external_user_id.is_(None),
        )
    )
    session.execute(stmt)

    query = select(Account).where(Account.asset_id == asset_id, Account.kind == kind)
    query = (
        query.where(Account.external_user_id == external_user_id)
        if is_user_kind
        else query.where(Account.external_user_id.is_(None))
    )
    account = session.execute(query).scalar_one_or_none()
    if account is None:
        raise AccountNotFound(f"account {asset_id}/{kind.value} could not be provisioned")
    return account


def get_user_accounts(
    session: Session, *, asset_id: str, external_user_id: str
) -> tuple[Account, Account]:
    """Return `(user_available, user_hold)`, creating them if this user is new."""
    available = get_or_create_account(
        session,
        asset_id=asset_id,
        kind=AccountKind.USER_AVAILABLE,
        external_user_id=external_user_id,
    )
    hold = get_or_create_account(
        session,
        asset_id=asset_id,
        kind=AccountKind.USER_HOLD,
        external_user_id=external_user_id,
    )
    return available, hold


def ensure_system_accounts(session: Session, asset_id: str) -> list[Account]:
    return [
        get_or_create_account(session, asset_id=asset_id, kind=kind)
        for kind in SYSTEM_ACCOUNT_KINDS
    ]


def get_system_account(session: Session, *, asset_id: str, kind: AccountKind) -> Account:
    if kind in USER_ACCOUNT_KINDS:
        raise AccountNotFound(f"{kind.value} is not a system account")
    account = session.execute(
        select(Account).where(
            Account.asset_id == asset_id,
            Account.kind == kind,
            Account.external_user_id.is_(None),
        )
    ).scalar_one_or_none()
    if account is None:
        raise AccountNotFound(f"system account {asset_id}/{kind.value} does not exist")
    return account


def lock_asset_gate(session: Session, asset_id: str) -> Account:
    """Serialize per-asset decisions on the asset's `hot_wallet` account row.

    The rolling-24h withdrawal cap is the control that bounds the hot wallet
    under a stolen-API-key burst. Summing the window without this lock lets N
    concurrent withdrawals across N different users each read the pre-cap
    total and all pass. Every deposit credit already contends on this row, so
    it costs nothing extra at this traffic level.

    Used by the M3 withdrawal path; it lives here because the lock ordering is
    part of the ledger's concurrency contract.
    """
    account = session.execute(
        select(Account)
        .where(
            Account.asset_id == asset_id,
            Account.kind == AccountKind.HOT_WALLET,
            Account.external_user_id.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if account is None:
        raise AccountNotFound(f"hot_wallet account for asset {asset_id} does not exist")
    return account


def _lock_accounts(session: Session, account_ids: Iterable[int]) -> dict[int, Account]:
    """Lock each account row with its own statement, ascending id.

    A single `... WHERE id IN (...) ORDER BY id FOR UPDATE` does not guarantee
    lock acquisition order, and deadlock freedom here depends on that order.
    """
    locked: dict[int, Account] = {}
    for account_id in sorted(account_ids):
        account = session.execute(
            select(Account).where(Account.id == account_id).with_for_update()
        ).scalar_one_or_none()
        if account is None:
            raise AccountNotFound(f"account {account_id} does not exist")
        locked[account_id] = account
    return locked


def post_entry(
    session: Session,
    *,
    kind: EntryKind,
    asset_id: str,
    source_ref: str,
    postings: Sequence[PostingPair],
    memo: str | None = None,
    reverses_entry_id: int | None = None,
) -> JournalEntry:
    """Write one balanced journal entry and its postings.

    `postings` is a sequence of `(account_id, amount)` with amounts signed
    positive for a debit and negative for a credit.

    Raises `UnbalancedEntry`, `ZeroPosting`, `AccountNotFound`, `AssetMismatch`
    before touching the database; `AlreadyPosted` when `(kind, source_ref)`
    exists; `InsufficientFunds` when a balance CHECK rejects the result. On any
    of those the session is left usable and this call has written nothing.
    """
    if len(postings) < 2:
        raise UnbalancedEntry("an entry needs at least two postings")

    deltas: dict[int, int] = {}
    for account_id, amount in postings:
        if amount == 0:
            raise ZeroPosting(f"posting to account {account_id} has amount 0")
        deltas[account_id] = deltas.get(account_id, 0) + amount

    total = sum(amount for _, amount in postings)
    if total != 0:
        raise UnbalancedEntry(f"postings sum to {total}, not 0")

    locked = _lock_accounts(session, deltas)
    for account in locked.values():
        if account.asset_id != asset_id:
            raise AssetMismatch(
                f"account {account.id} holds {account.asset_id}, entry is for {asset_id}"
            )

    try:
        with session.begin_nested():
            entry = JournalEntry(
                kind=kind,
                asset_id=asset_id,
                source_ref=source_ref,
                reverses_entry_id=reverses_entry_id,
                memo=memo,
            )
            session.add(entry)
            session.flush()

            session.add_all(
                [
                    Posting(entry_id=entry.id, account_id=account_id, amount=amount)
                    for account_id, amount in postings
                ]
            )
            for account_id, delta in sorted(deltas.items()):
                if delta == 0:
                    continue
                # SQL-side arithmetic: the row is locked, and this keeps the
                # update correct even if the in-session copy were stale.
                locked[account_id].balance = Account.balance + delta
            session.flush()
    except IntegrityError as exc:
        constraint = _constraint_name(exc)
        if constraint == "ux_entry_source":
            existing = session.execute(
                select(JournalEntry).where(
                    JournalEntry.kind == kind, JournalEntry.source_ref == source_ref
                )
            ).scalar_one_or_none()
            raise AlreadyPosted(kind, source_ref, existing) from exc
        if constraint in ("no_overdraft", "no_negative_asset"):
            raise InsufficientFunds(constraint) from exc
        raise

    return entry


def seed_assets(session: Session, specs: Sequence[AssetSpec]) -> list[str]:
    """Insert missing `assets` rows and their system accounts.

    Idempotent: an existing row is never updated, because the database — not
    the environment — is the source of truth once an asset exists.
    """
    inserted: list[str] = []
    for spec in specs:
        result = session.execute(
            pg_insert(Asset)
            .values(
                id=spec.id,
                display_name=spec.display_name,
                decimals=spec.decimals,
                unit_name=spec.unit_name,
                btcpay_payment_method=spec.btcpay_payment_method,
                withdrawal_auto_limit=spec.withdrawal_auto_limit,
                withdrawal_daily_cap=spec.withdrawal_daily_cap,
                withdrawal_user_daily_cap=spec.withdrawal_user_daily_cap,
                withdrawal_min=spec.withdrawal_min,
                withdrawal_flat_fee=spec.withdrawal_flat_fee,
            )
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(Asset.id)
        )
        if result.scalar_one_or_none() is not None:
            inserted.append(spec.id)
        ensure_system_accounts(session, spec.id)
    return inserted
