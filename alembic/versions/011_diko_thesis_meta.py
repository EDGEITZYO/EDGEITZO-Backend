"""DIKO 학위논문 메타 컬럼 추가

- degree: 학위구분 (국내석사/국내박사)
- affiliation: 저자소속기관(학위수여기관)
- publisher: 발행기관(학위수여기관)
- fulltext_flag: 원문공개 여부

Revision ID: 011
Revises: 010
Create Date: 2026-05-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("degree", sa.String(30), nullable=True))
    op.add_column("papers", sa.Column("affiliation", sa.String(500), nullable=True))
    op.add_column("papers", sa.Column("publisher", sa.String(500), nullable=True))
    op.add_column("papers", sa.Column("fulltext_flag", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "fulltext_flag")
    op.drop_column("papers", "publisher")
    op.drop_column("papers", "affiliation")
    op.drop_column("papers", "degree")
