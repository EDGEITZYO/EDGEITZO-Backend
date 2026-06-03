"""papers 테이블에 pubdate 컬럼 추가

Revision ID: 008a
Revises: 007
Create Date: 2026-05-26
"""
from __future__ import annotations

from alembic import op

revision = "008a"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS pubdate VARCHAR(10)")


def downgrade() -> None:
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS pubdate")
