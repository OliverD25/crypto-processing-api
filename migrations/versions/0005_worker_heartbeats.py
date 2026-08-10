"""Worker heartbeats.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-10

`/healthz` deliberately answers for the API process and its database only, so a
dead worker cannot make the API look down. But a dead worker is a serious
condition in its own right: webhooks stop being processed and reconciliation —
the thing that actually makes deposits correct — stops running, while every
read endpoint keeps answering 200.

Each job writes its name and the time it last finished here, and `/readyz`
reports how stale that is.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE worker_heartbeats (
            job_name     TEXT PRIMARY KEY,
            last_run_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_result  TEXT,
            last_error   TEXT
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
