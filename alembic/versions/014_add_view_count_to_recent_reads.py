"""recent_reads.view_count 컬럼 추가

Revision ID: 014
Revises: 013
Create Date: 2026-06-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recent_reads",
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("recent_reads", "view_count")
