"""repurpose user_keyword_maps table for last-anchor session resume

키워드맵이 LLM 트리 영속 저장 방식에서 매 요청 즉시 계산 방식으로 바뀌면서
research_field/tree 컬럼(4축 트리 원본 저장용)이 더 이상 필요 없어짐.
같은 테이블/마이그레이션 계보를 재사용해 "마지막 조회 앵커" 세션 재개용으로 컬럼만 교체.

Revision ID: 019
Revises: 018
Create Date: 2026-07-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("user_keyword_maps", "tree")
    op.alter_column("user_keyword_maps", "research_field", new_column_name="last_anchor_key")
    op.add_column("user_keyword_maps", sa.Column("last_anchor_name_ko", sa.String(), nullable=True))
    op.add_column("user_keyword_maps", sa.Column("last_anchor_name_en", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_keyword_maps", "last_anchor_name_en")
    op.drop_column("user_keyword_maps", "last_anchor_name_ko")
    op.alter_column("user_keyword_maps", "last_anchor_key", new_column_name="research_field")
    op.add_column("user_keyword_maps", sa.Column("tree", postgresql.JSONB(), nullable=True))
