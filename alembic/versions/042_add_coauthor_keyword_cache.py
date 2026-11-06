"""add coauthor_keyword_cache

Revision ID: 042
Revises: 041

공저자 페이지의 공동 연구 키워드 중 LLM이 판단한 결과를 저장한다. LLM을 쓴 쌍만 행이 생긴다.
  ranked_keywords  겹치는 키워드가 표시 개수보다 적을 때 LLM이 함께 쓴 논문의 키워드 중 고른 순위
                   (대상: 2편 이상 쌍 중 4,727쌍, 2026-11-07 실측)
  keyword_groups   고른 키워드에 한글·영문이 섞였을 때 LLM이 뜻이 같다고 묶은 것({"keywords": 물어본 목록, "groups": [[...]]})
둘은 따로 채워지므로 각각 null일 수 있다.

두 사람의 순서와 무관하게 같은 논문 집합이라 (작은 ID, 큰 ID)로 저장한다.
무효화는 researcher_flow_cache와 같은 두 가지: prompt_version, paper_signature(함께 쓴 논문·키워드가 바뀌면 달라짐).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coauthor_keyword_cache",
        sa.Column("researcher_a", sa.String(100), primary_key=True),
        sa.Column("researcher_b", sa.String(100), primary_key=True),
        sa.Column("prompt_version", sa.String(20), primary_key=True),
        sa.Column("paper_signature", sa.String(64), nullable=False),
        sa.Column("ranked_keywords", postgresql.JSONB(), nullable=True),
        sa.Column("keyword_groups", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("coauthor_keyword_cache")
