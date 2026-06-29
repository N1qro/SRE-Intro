"""add email column to events

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-29 00:00:01.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("events", sa.Column("email", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "email")
