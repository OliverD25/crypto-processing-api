"""Address reservation windows.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-10

The USDt plugin does not derive a fresh address per invoice. It reserves one
from a pre-provisioned pool and releases it afterwards, so the same TRON
address serves different users over time.

That makes late USDT payments dangerous in a way BTC's are not. A payment that
arrives after the window closed may be attributed by BTCPay to whichever
invoice holds the address *now* — a different user. BTCPay does not flag it,
because from its point of view it is an in-window payment on the current
invoice.

These columns record which deposit held which address between which instants.
They are what makes manual attribution possible at all: with them an operator
can take a TRON transaction timestamp and find the deposit that actually owned
the address at that moment. Without them the question has no answer.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE deposits
            ADD COLUMN address_reserved_from  TIMESTAMPTZ,
            ADD COLUMN address_reserved_until TIMESTAMPTZ
        """
    )
    # The attribution runbook's query: "which deposits held this address, and
    # when?" — newest first.
    op.execute(
        """
        CREATE INDEX ix_deposits_address ON deposits (asset_id, address, created_at DESC)
            WHERE address IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_deposits_address")
    op.execute(
        """
        ALTER TABLE deposits
            DROP COLUMN IF EXISTS address_reserved_until,
            DROP COLUMN IF EXISTS address_reserved_from
        """
    )
