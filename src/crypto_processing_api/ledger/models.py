"""SQLAlchemy 2.0 declarative models for every table in the ledger database.

The authoritative DDL is Alembic revision 0001; these models mirror it. All
monetary columns are BIGINT counts of the asset's smallest unit (satoshi,
micro-USDT) — never floats, never decimals.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AccountKind(StrEnum):
    USER_AVAILABLE = "user_available"
    USER_HOLD = "user_hold"
    HOT_WALLET = "hot_wallet"
    PAYOUTS_IN_FLIGHT = "payouts_in_flight"
    FEE_INCOME = "fee_income"
    NETWORK_FEE_EXPENSE = "network_fee_expense"
    EXTERNAL = "external"
    USER_DEFICIT = "user_deficit"


class EntryKind(StrEnum):
    DEPOSIT_CREDIT = "deposit_credit"
    WITHDRAWAL_HOLD = "withdrawal_hold"
    WITHDRAWAL_SETTLE = "withdrawal_settle"
    WITHDRAWAL_RELEASE = "withdrawal_release"
    ADJUSTMENT = "adjustment"
    REVERSAL = "reversal"


class DepositStatus(StrEnum):
    CREATING = "creating"
    PENDING = "pending"
    CONFIRMING = "confirming"
    SETTLED = "settled"
    EXPIRED = "expired"
    REVIEW = "review"
    DISMISSED = "dismissed"
    FAILED = "failed"


class WithdrawalStatus(StrEnum):
    REQUESTED = "requested"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    BROADCAST = "broadcast"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    REFUNDED = "refunded"


class NormalSide(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


#: Which side of the book each account kind sits on. Liabilities to users are
#: credit-normal (their raw balance is negative); custody assets are
#: debit-normal. `user_deficit` is a debit-normal receivable: it exists so a
#: post-credit reorg loss on an already-spent balance can be booked at all.
NORMAL_SIDE_BY_KIND: dict[AccountKind, NormalSide] = {
    AccountKind.USER_AVAILABLE: NormalSide.CREDIT,
    AccountKind.USER_HOLD: NormalSide.CREDIT,
    AccountKind.HOT_WALLET: NormalSide.DEBIT,
    AccountKind.PAYOUTS_IN_FLIGHT: NormalSide.DEBIT,
    AccountKind.FEE_INCOME: NormalSide.CREDIT,
    AccountKind.NETWORK_FEE_EXPENSE: NormalSide.DEBIT,
    AccountKind.EXTERNAL: NormalSide.CREDIT,
    AccountKind.USER_DEFICIT: NormalSide.DEBIT,
}

#: Account kinds that carry an `external_user_id`.
USER_ACCOUNT_KINDS: frozenset[AccountKind] = frozenset(
    {AccountKind.USER_AVAILABLE, AccountKind.USER_HOLD}
)

#: Kinds exempt from the balance-sign CHECK constraints. Corrections and reorg
#: losses legitimately swing both signs on these two.
UNBOUNDED_ACCOUNT_KINDS: frozenset[AccountKind] = frozenset(
    {AccountKind.EXTERNAL, AccountKind.USER_DEFICIT}
)


def _pg_enum(python_enum: type[StrEnum], name: str) -> ENUM:
    return ENUM(
        python_enum,
        name=name,
        create_type=False,
        values_callable=lambda e: [member.value for member in e],
    )


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    decimals: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    unit_name: Mapped[str] = mapped_column(Text, nullable=False)
    btcpay_payment_method: Mapped[str] = mapped_column(Text, nullable=False)
    withdrawal_auto_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    withdrawal_daily_cap: Mapped[int] = mapped_column(BigInteger, nullable=False)
    withdrawal_user_daily_cap: Mapped[int | None] = mapped_column(BigInteger)
    withdrawal_min: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    withdrawal_flat_fee: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        # An 18-decimal asset would overflow BIGINT above ~9.2e9 tokens. Adding
        # one has to be a deliberate migration, not a config row.
        CheckConstraint("decimals BETWEEN 0 AND 8", name="assets_decimals_range"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset_id: Mapped[str] = mapped_column(Text, ForeignKey("assets.id"), nullable=False)
    kind: Mapped[AccountKind] = mapped_column(_pg_enum(AccountKind, "account_kind"), nullable=False)
    external_user_id: Mapped[str | None] = mapped_column(Text)
    normal_side: Mapped[NormalSide] = mapped_column(String, nullable=False)
    balance: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("normal_side IN ('debit','credit')", name="accounts_normal_side"),
        CheckConstraint(
            "(kind IN ('user_available','user_hold')) = (external_user_id IS NOT NULL)",
            name="user_acct_has_user",
        ),
        CheckConstraint(
            "kind IN ('external','user_deficit') OR normal_side <> 'credit' OR balance <= 0",
            name="no_overdraft",
        ),
        CheckConstraint(
            "kind IN ('external','user_deficit') OR normal_side <> 'debit' OR balance >= 0",
            name="no_negative_asset",
        ),
        Index(
            "ux_accounts_user",
            "asset_id",
            "kind",
            "external_user_id",
            unique=True,
            postgresql_where=text("external_user_id IS NOT NULL"),
        ),
        Index(
            "ux_accounts_system",
            "asset_id",
            "kind",
            unique=True,
            postgresql_where=text("external_user_id IS NULL"),
        ),
    )


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[EntryKind] = mapped_column(_pg_enum(EntryKind, "entry_kind"), nullable=False)
    asset_id: Mapped[str] = mapped_column(Text, ForeignKey("assets.id"), nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    reverses_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id")
    )
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Ledger-level idempotency: a replayed webhook, a poller racing that
        # webhook and an operator retry all collide here.
        UniqueConstraint("kind", "source_ref", name="ux_entry_source"),
    )


class Posting(Base):
    __tablename__ = "postings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount <> 0", name="postings_amount_nonzero"),
        Index("ix_postings_account", "account_id", text("id DESC")),
        Index("ix_postings_entry", "entry_id"),
    )


class Deposit(Base):
    __tablename__ = "deposits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    asset_id: Mapped[str] = mapped_column(Text, ForeignKey("assets.id"), nullable=False)
    btcpay_invoice_id: Mapped[str | None] = mapped_column(Text, unique=True)
    amount_expected: Mapped[int | None] = mapped_column(BigInteger)
    amount_credited: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    status: Mapped[DepositStatus] = mapped_column(
        _pg_enum(DepositStatus, "deposit_status"),
        nullable=False,
        server_default=text("'creating'"),
    )
    address: Mapped[str | None] = mapped_column(Text)
    checkout_link: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_payment_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_deposits_user", "external_user_id", text("created_at DESC")),
        Index(
            "ix_deposits_active",
            "status",
            postgresql_where=text("status IN ('creating','pending','confirming','review')"),
        ),
    )


class DepositPayment(Base):
    __tablename__ = "deposit_payments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("deposits.id"), nullable=False)
    btcpay_payment_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    after_expiration: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    ledger_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id")
    )
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("amount > 0", name="deposit_payments_amount_positive"),
        UniqueConstraint("deposit_id", "btcpay_payment_id", name="ux_deposit_payment"),
    )


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    external_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    asset_id: Mapped[str] = mapped_column(Text, ForeignKey("assets.id"), nullable=False)
    destination_address: Mapped[str] = mapped_column(Text, nullable=False)
    amount_gross: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_amount: Mapped[int | None] = mapped_column(BigInteger)
    amount_net: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[WithdrawalStatus] = mapped_column(
        _pg_enum(WithdrawalStatus, "withdrawal_status"),
        nullable=False,
        server_default=text("'requested'"),
    )
    approval_mode: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    backend_ref: Mapped[str | None] = mapped_column(Text, unique=True)
    txid: Mapped[str | None] = mapped_column(Text)
    hold_entry_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("journal_entries.id"))
    settle_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id")
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount_gross > 0", name="withdrawals_gross_positive"),
        CheckConstraint("approval_mode IN ('auto','manual')", name="withdrawals_approval_mode"),
        Index("ix_withdrawals_user", "external_user_id", text("created_at DESC")),
        Index(
            "ix_withdrawals_pending",
            "status",
            postgresql_where=text(
                "status IN ('pending_approval','approved','submitting','submitted','broadcast')"
            ),
        ),
        # One on-chain transaction can settle at most one withdrawal: an
        # operator pasting a previous withdrawal's txid must be rejected.
        Index(
            "ux_withdrawals_txid", "txid", unique=True, postgresql_where=text("txid IS NOT NULL")
        ),
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    btcpay_invoice_id: Mapped[str | None] = mapped_column(Text)
    btcpay_payout_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'received'"))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    processing_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('received','processed','failed','ignored','orphaned')",
            name="webhook_events_status",
        ),
        Index("ix_webhook_pending", "received_at", postgresql_where=text("status = 'received'")),
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'in_progress'"))
    resource_id: Mapped[str | None] = mapped_column(Text)
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("state IN ('in_progress','completed')", name="idempotency_keys_state"),
        Index("ix_idem_created", "created_at"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("scope IN ('readwrite','admin')", name="api_keys_scope"),)


class OutboundEvent(Base):
    __tablename__ = "outbound_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('pending','delivered','dead')", name="outbound_events_status"),
        Index("ix_outbound_due", "next_attempt_at", postgresql_where=text("status = 'pending'")),
    )
