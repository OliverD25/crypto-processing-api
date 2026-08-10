"""In-flight payout accounting and the release attestation.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-10

Two additions, both from the adversarial review.

`withdrawal_submit` is a new entry kind. Between broadcast and confirmation the
coins have left the BTCPay wallet but the ledger still counted them as
hot_wallet, so "ledger versus live wallet" diverged by a growing, unexplained
amount and the insolvency alert needed a hand-tuned epsilon to stay quiet. With
a submit entry moving the committed amount into `payouts_in_flight`, the
expected divergence is derived rather than guessed.

`release_attestation` exists because releasing a hold after broadcast is a
double-pay path: the user gets their balance back and the original transaction
confirms anyway. From `submitted` onward a release is an admin action that has
to say, in writing and on the row, why the money is not going to arrive.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL 12+ allows ADD VALUE inside a transaction as long as the new
    # value is not used in the same transaction. Nothing here uses it.
    op.execute("ALTER TYPE entry_kind ADD VALUE IF NOT EXISTS 'withdrawal_submit'")

    op.execute(
        """
        ALTER TABLE withdrawals
            ADD COLUMN submit_entry_id     BIGINT REFERENCES journal_entries(id),
            ADD COLUMN release_entry_id    BIGINT REFERENCES journal_entries(id),
            ADD COLUMN submitted_at        TIMESTAMPTZ,
            ADD COLUMN rejected_by         TEXT,
            ADD COLUMN released_by         TEXT,
            ADD COLUMN release_attestation TEXT
        """
    )

    # Stuck-`submitting` resolution matches unclaimed payouts by destination and
    # amount inside a time window before it will consider anything.
    op.execute(
        """
        CREATE INDEX ix_withdrawals_destination
            ON withdrawals (asset_id, destination_address, created_at DESC)
        """
    )
    # The rolling 24h velocity cap sums this table rather than keeping a
    # counter, so the sum has to stay cheap.
    op.execute("CREATE INDEX ix_withdrawals_velocity ON withdrawals (asset_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_withdrawals_velocity")
    op.execute("DROP INDEX IF EXISTS ix_withdrawals_destination")
    op.execute(
        """
        ALTER TABLE withdrawals
            DROP COLUMN IF EXISTS release_attestation,
            DROP COLUMN IF EXISTS released_by,
            DROP COLUMN IF EXISTS rejected_by,
            DROP COLUMN IF EXISTS submitted_at,
            DROP COLUMN IF EXISTS release_entry_id,
            DROP COLUMN IF EXISTS submit_entry_id
        """
    )
    # Postgres cannot drop a value from an enum; 'withdrawal_submit' stays.
