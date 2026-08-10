"""Asset extension columns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-10

Three facts about an asset lived in Python dicts keyed by asset id: which
currency its invoice is denominated in, how long that invoice may stay open,
and whether its deposit addresses come from a shared pool. All three are data
about a specific asset, and keeping them in code meant adding an asset required
editing three unrelated dicts and hoping none was missed.

They move here. The registry that follows holds *behavior* — validators, fee
policies, custody sources — and the split is deliberate: data in the database,
behavior in code. A behavior name stored as a string in a row is a way for the
database and the deployed code to disagree about what runs.

Defaults reproduce v0.1.0 exactly, which is the only acceptable outcome for an
upgrade on a live custodial ledger:

- `pooled_addresses` defaults false, then USDT_TRC20 is set true — matching
  POOLED_ASSETS = frozenset({"USDT_TRC20"}).
- `invoice_currency` is backfilled BTC->'BTC', USDT_TRC20->'USDT', matching
  INVOICE_CURRENCY. It stays nullable so the old `.get(asset.id, asset.id)`
  fallback keeps its meaning for an asset nobody has backfilled.
- `deposit_expiry_minutes` is NULL for both, meaning "use the global default",
  which is what `_expiry_minutes` did for BTC. USDT is left NULL on purpose:
  its per-asset value comes from settings, and writing that number into the row
  here would freeze today's configuration into data that outlives it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE assets
            ADD COLUMN pooled_addresses       BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN invoice_currency       TEXT,
            ADD COLUMN deposit_expiry_minutes INTEGER
        """
    )
    op.execute(
        """
        ALTER TABLE assets
            ADD CONSTRAINT assets_expiry_positive
            CHECK (deposit_expiry_minutes IS NULL OR deposit_expiry_minutes > 0)
        """
    )
    # Data migration, not a default: these two rows exist on every deployment
    # and their behaviour must not change across the upgrade.
    op.execute("UPDATE assets SET pooled_addresses = true WHERE id = 'USDT_TRC20'")
    op.execute("UPDATE assets SET invoice_currency = 'BTC' WHERE id = 'BTC'")
    op.execute("UPDATE assets SET invoice_currency = 'USDT' WHERE id = 'USDT_TRC20'")


def downgrade() -> None:
    op.execute("ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_expiry_positive")
    op.execute(
        """
        ALTER TABLE assets
            DROP COLUMN IF EXISTS deposit_expiry_minutes,
            DROP COLUMN IF EXISTS invoice_currency,
            DROP COLUMN IF EXISTS pooled_addresses
        """
    )
