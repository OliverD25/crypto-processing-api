"""Initial ledger schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-10

The whole database in one revision: chart of accounts, append-only journal,
deposits, withdrawals, webhook ingress, client idempotency, API keys and the
outbound event queue.

Written as explicit SQL rather than Alembic operations. Every CHECK, partial
index and trigger below is a money-safety mechanism argued for in
docs/design/, and a reviewer has to be able to read them as the design states
them.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE assets (
            id                        TEXT PRIMARY KEY,
            display_name              TEXT NOT NULL,
            decimals                  SMALLINT NOT NULL,
            unit_name                 TEXT NOT NULL,
            btcpay_payment_method     TEXT NOT NULL,
            withdrawal_auto_limit     BIGINT NOT NULL,
            withdrawal_daily_cap      BIGINT NOT NULL,
            withdrawal_user_daily_cap BIGINT,
            withdrawal_min            BIGINT NOT NULL DEFAULT 1,
            withdrawal_flat_fee       BIGINT NOT NULL DEFAULT 0,
            enabled                   BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT assets_decimals_range CHECK (decimals BETWEEN 0 AND 8)
        )
        """
    )

    op.execute(
        """
        CREATE TYPE account_kind AS ENUM (
            'user_available','user_hold',
            'hot_wallet','payouts_in_flight',
            'fee_income','network_fee_expense',
            'external',
            'user_deficit'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE accounts (
            id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            asset_id         TEXT NOT NULL REFERENCES assets(id),
            kind             account_kind NOT NULL,
            external_user_id TEXT,
            normal_side      TEXT NOT NULL,
            balance          BIGINT NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT accounts_normal_side CHECK (normal_side IN ('debit','credit')),
            CONSTRAINT user_acct_has_user CHECK (
                (kind IN ('user_available','user_hold')) = (external_user_id IS NOT NULL)),
            -- Liabilities may never flip to a debit balance: this is the
            -- database-level backstop against a double spend surviving an
            -- application bug.
            CONSTRAINT no_overdraft CHECK (
                kind IN ('external','user_deficit')
                OR normal_side <> 'credit' OR balance <= 0),
            CONSTRAINT no_negative_asset CHECK (
                kind IN ('external','user_deficit')
                OR normal_side <> 'debit' OR balance >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_accounts_user ON accounts (asset_id, kind, external_user_id)
            WHERE external_user_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_accounts_system ON accounts (asset_id, kind)
            WHERE external_user_id IS NULL
        """
    )

    op.execute(
        """
        CREATE TYPE entry_kind AS ENUM (
            'deposit_credit','withdrawal_hold','withdrawal_settle',
            'withdrawal_release','adjustment','reversal'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE journal_entries (
            id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind              entry_kind NOT NULL,
            asset_id          TEXT NOT NULL REFERENCES assets(id),
            source_ref        TEXT NOT NULL,
            reverses_entry_id BIGINT REFERENCES journal_entries(id),
            memo              TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ux_entry_source UNIQUE (kind, source_ref)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE postings (
            id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            entry_id   BIGINT NOT NULL REFERENCES journal_entries(id),
            account_id BIGINT NOT NULL REFERENCES accounts(id),
            amount     BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT postings_amount_nonzero CHECK (amount <> 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_postings_account ON postings (account_id, id DESC)")
    op.execute("CREATE INDEX ix_postings_entry ON postings (entry_id)")

    # Deferred so a multi-posting entry is legal mid-transaction and only has
    # to balance at commit. post_entry() also asserts this in Python; the
    # trigger is what makes a hand-written INSERT impossible to get wrong.
    op.execute(
        """
        CREATE FUNCTION assert_entry_balanced() RETURNS trigger AS $$
        DECLARE
            entry_total BIGINT;
        BEGIN
            SELECT COALESCE(SUM(amount), 0) INTO entry_total
                FROM postings WHERE entry_id = NEW.entry_id;
            IF entry_total <> 0 THEN
                RAISE EXCEPTION 'journal entry % does not balance: sum = %',
                    NEW.entry_id, entry_total
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_entry_balanced
            AFTER INSERT ON postings
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced()
        """
    )

    # Append-only. A correction is a 'reversal' entry that references the
    # entry it reverses; nothing edits history in place.
    op.execute(
        """
        CREATE FUNCTION reject_ledger_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed, post a reversal entry instead',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_postings_immutable
            BEFORE UPDATE OR DELETE ON postings
            FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_journal_entries_immutable
            BEFORE UPDATE OR DELETE ON journal_entries
            FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()
        """
    )

    op.execute(
        """
        CREATE TYPE deposit_status AS ENUM (
            'creating','pending','confirming','settled',
            'expired','review','dismissed','failed'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE deposits (
            id                   UUID PRIMARY KEY,
            external_user_id     TEXT NOT NULL,
            asset_id             TEXT NOT NULL REFERENCES assets(id),
            btcpay_invoice_id    TEXT UNIQUE,
            amount_expected      BIGINT,
            amount_credited      BIGINT NOT NULL DEFAULT 0,
            status               deposit_status NOT NULL DEFAULT 'creating',
            address              TEXT,
            checkout_link        TEXT,
            expires_at           TIMESTAMPTZ,
            last_payment_seen_at TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_deposits_user ON deposits (external_user_id, created_at DESC)")
    op.execute(
        """
        CREATE INDEX ix_deposits_active ON deposits (status)
            WHERE status IN ('creating','pending','confirming','review')
        """
    )

    op.execute(
        """
        CREATE TABLE deposit_payments (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deposit_id        UUID NOT NULL REFERENCES deposits(id),
            btcpay_payment_id TEXT NOT NULL,
            amount            BIGINT NOT NULL,
            after_expiration  BOOLEAN NOT NULL DEFAULT FALSE,
            ledger_entry_id   BIGINT REFERENCES journal_entries(id),
            credited_at       TIMESTAMPTZ,
            resolved_by       TEXT,
            CONSTRAINT deposit_payments_amount_positive CHECK (amount > 0),
            CONSTRAINT ux_deposit_payment UNIQUE (deposit_id, btcpay_payment_id)
        )
        """
    )

    op.execute(
        """
        CREATE TYPE withdrawal_status AS ENUM (
            'requested','pending_approval','approved','rejected',
            'submitting','submitted','broadcast','confirmed','failed','refunded'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE withdrawals (
            id                  UUID PRIMARY KEY,
            external_user_id    TEXT NOT NULL,
            asset_id            TEXT NOT NULL REFERENCES assets(id),
            destination_address TEXT NOT NULL,
            amount_gross        BIGINT NOT NULL,
            fee_amount          BIGINT,
            amount_net          BIGINT,
            status              withdrawal_status NOT NULL DEFAULT 'requested',
            approval_mode       TEXT,
            approved_by         TEXT,
            backend             TEXT NOT NULL,
            backend_ref         TEXT UNIQUE,
            txid                TEXT,
            hold_entry_id       BIGINT REFERENCES journal_entries(id),
            settle_entry_id     BIGINT REFERENCES journal_entries(id),
            failure_reason      TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT withdrawals_gross_positive CHECK (amount_gross > 0),
            CONSTRAINT withdrawals_approval_mode CHECK (approval_mode IN ('auto','manual'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_withdrawals_user ON withdrawals (external_user_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE INDEX ix_withdrawals_pending ON withdrawals (status)
            WHERE status IN ('pending_approval','approved','submitting','submitted','broadcast')
        """
    )
    # One on-chain transaction settles at most one withdrawal. Without this an
    # operator can paste a previous withdrawal's txid and mark a send complete
    # that never happened.
    op.execute(
        "CREATE UNIQUE INDEX ux_withdrawals_txid ON withdrawals (txid) WHERE txid IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE webhook_events (
            id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            dedup_key         TEXT NOT NULL UNIQUE,
            delivery_id       TEXT NOT NULL,
            event_type        TEXT NOT NULL,
            btcpay_invoice_id TEXT,
            btcpay_payout_id  TEXT,
            payload           JSONB NOT NULL,
            status            TEXT NOT NULL DEFAULT 'received',
            received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at      TIMESTAMPTZ,
            attempts          SMALLINT NOT NULL DEFAULT 0,
            processing_error  TEXT,
            CONSTRAINT webhook_events_status CHECK (
                status IN ('received','processed','failed','ignored','orphaned'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_webhook_pending ON webhook_events (received_at) WHERE status = 'received'"
    )

    # `resource_id` lets a retry that takes over a stale in_progress row learn
    # which deposit or withdrawal it already owns, instead of receiving 409 for
    # the whole TTL.
    op.execute(
        """
        CREATE TABLE idempotency_keys (
            key             TEXT NOT NULL,
            endpoint        TEXT NOT NULL,
            request_hash    TEXT NOT NULL,
            state           TEXT NOT NULL DEFAULT 'in_progress',
            resource_id     TEXT,
            response_status SMALLINT,
            response_body   JSONB,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (key, endpoint),
            CONSTRAINT idempotency_keys_state CHECK (state IN ('in_progress','completed'))
        )
        """
    )
    op.execute("CREATE INDEX ix_idem_created ON idempotency_keys (created_at)")

    op.execute(
        """
        CREATE TABLE api_keys (
            id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            key_id       TEXT NOT NULL UNIQUE,
            key_hash     TEXT NOT NULL,
            name         TEXT NOT NULL,
            scope        TEXT NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at   TIMESTAMPTZ,
            revoked_at   TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            CONSTRAINT api_keys_scope CHECK (scope IN ('readwrite','admin'))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE outbound_events (
            id              UUID PRIMARY KEY,
            event_type      TEXT NOT NULL,
            payload         JSONB NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        SMALLINT NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT outbound_events_status CHECK (status IN ('pending','delivered','dead'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_outbound_due ON outbound_events (next_attempt_at)
            WHERE status = 'pending'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outbound_events")
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
    op.execute("DROP TABLE IF EXISTS webhook_events")
    op.execute("DROP TABLE IF EXISTS withdrawals")
    op.execute("DROP TABLE IF EXISTS deposit_payments")
    op.execute("DROP TABLE IF EXISTS deposits")
    op.execute("DROP TRIGGER IF EXISTS trg_journal_entries_immutable ON journal_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_postings_immutable ON postings")
    op.execute("DROP TRIGGER IF EXISTS trg_entry_balanced ON postings")
    op.execute("DROP TABLE IF EXISTS postings")
    op.execute("DROP TABLE IF EXISTS journal_entries")
    op.execute("DROP TABLE IF EXISTS accounts")
    op.execute("DROP TABLE IF EXISTS assets")
    op.execute("DROP FUNCTION IF EXISTS reject_ledger_mutation()")
    op.execute("DROP FUNCTION IF EXISTS assert_entry_balanced()")
    op.execute("DROP TYPE IF EXISTS withdrawal_status")
    op.execute("DROP TYPE IF EXISTS deposit_status")
    op.execute("DROP TYPE IF EXISTS entry_kind")
    op.execute("DROP TYPE IF EXISTS account_kind")
