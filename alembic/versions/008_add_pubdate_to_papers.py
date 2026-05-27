"""papers 테이블에 pubdate 컬럼 추가

Revision ID: 008
Revises: 007
Create Date: 2026-05-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("pubdate", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "pubdate")
