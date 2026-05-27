"""journals 테이블에 kci_field 컬럼 추가

Revision ID: 012
Revises: 011
Create Date: 2026-05-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("journals", sa.Column("kci_field", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("journals", "kci_field")
