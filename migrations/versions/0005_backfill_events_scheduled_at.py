"""backfill events.scheduled_at

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-17 00:00:02.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE events SET scheduled_at = event_date WHERE scheduled_at IS NULL"
    )
    op.alter_column("events", "scheduled_at", nullable=False)


def downgrade() -> None:
    op.alter_column("events", "scheduled_at", nullable=True)
