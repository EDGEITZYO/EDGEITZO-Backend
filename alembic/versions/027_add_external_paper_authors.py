"""researcher_external_papers에 저자 목록 추가

Revision ID: 027
Revises: 026
Create Date: 2026-08-22

expand 단계가 articleSearch 응답의 저자를 파싱해놓고 저장하지 않아,
「함께 연구한 사람들」이 코퍼스 논문(전체의 10%)만 반영하고 있었다.
저자를 저장하면 공저 관계가 8천 건에서 12만 건 수준으로 늘어난다.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "researcher_external_papers",
        sa.Column("authors", postgresql.ARRAY(sa.String(length=300)), nullable=True),
    )
    op.add_column(
        "researcher_external_papers",
        sa.Column("author_institutions", postgresql.ARRAY(sa.String(length=500)), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("researcher_external_papers", "author_institutions")
    op.drop_column("researcher_external_papers", "authors")
