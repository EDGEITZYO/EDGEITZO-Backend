"""recent_reads.view_count 컬럼 추가

Revision ID: 014
Revises: 013
Create Date: 2026-06-03
"""
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE recent_reads "
        "ADD COLUMN IF NOT EXISTS view_count INTEGER NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE recent_reads DROP COLUMN IF EXISTS view_count")
