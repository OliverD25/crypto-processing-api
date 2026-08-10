"""Deposit monitoring window and unattributed-receive alerts.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10

Both changes exist for the same failure: the afterlife of a deposit address.

`deposits.monitoring_expires_at` records the window BTCPay itself will keep
attributing payments to the invoice. Polling past it is theatre — BTCPay no
longer links payments to that invoice — so the sweep needs to know where the
window ends rather than guess with a fixed number of days.

`wallet_txo_alerts` holds the only thing that catches a payment to a long-dead
address: a wallet transaction that credited the hot wallet and matches no
deposit payment. Job C's aggregate check cannot see it, because a surplus
reads as healthy.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE deposits ADD COLUMN monitoring_expires_at TIMESTAMPTZ")
    op.execute(
        """
        CREATE INDEX ix_deposits_monitoring ON deposits (monitoring_expires_at)
            WHERE status IN ('settled','expired','review')
        """
    )

    op.execute(
        """
        CREATE TABLE wallet_txo_alerts (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            asset_id      TEXT NOT NULL REFERENCES assets(id),
            txid          TEXT NOT NULL,
            amount        BIGINT NOT NULL,
            confirmations INTEGER,
            status        TEXT NOT NULL DEFAULT 'open',
            note          TEXT,
            detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            resolved_at   TIMESTAMPTZ,
            resolved_by   TEXT,
            CONSTRAINT wallet_txo_alerts_status CHECK (status IN ('open','resolved','ignored')),
            CONSTRAINT ux_wallet_txo_alert UNIQUE (asset_id, txid)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_wallet_txo_open ON wallet_txo_alerts (detected_at) WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wallet_txo_alerts")
    op.execute("DROP INDEX IF EXISTS ix_deposits_monitoring")
    op.execute("ALTER TABLE deposits DROP COLUMN IF EXISTS monitoring_expires_at")
