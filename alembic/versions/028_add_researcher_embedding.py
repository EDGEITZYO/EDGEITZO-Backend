"""연구자 임베딩 컬럼 추가

Revision ID: 028
Revises: 027
Create Date: 2026-08-22

명세서 「노드 그래프 뷰」는 노드 간 거리를 "키워드 공유"로 재라고 하는데, 실측하니
분야 검색 결과 안에서 쌍의 45%가 키워드를 하나도 공유하지 않아 그래프 절반이
"무한히 멂"으로 그려진다. 의미 기반 유사도를 쓰려고 연구자별 벡터를 저장한다.

pgvector가 없어 REAL[]로 둔다 — 연구자 2,753명이면 전수 코사인이 수 ms라 인덱스가 필요 없다.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("researchers", sa.Column("embedding", postgresql.ARRAY(sa.REAL()), nullable=True))
    op.add_column("researchers", sa.Column("embedding_graph", postgresql.ARRAY(sa.REAL()), nullable=True))
    op.add_column("researchers", sa.Column("embedding_model", sa.String(length=100), nullable=True))
    op.add_column("researchers", sa.Column("embedding_text", sa.Text(), nullable=True))
    op.add_column("researchers", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("embedded_at", "embedding_text", "embedding_model", "embedding_graph", "embedding"):
        op.drop_column("researchers", col)
