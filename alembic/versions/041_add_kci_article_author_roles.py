"""add kci_article_author_roles

Revision ID: 041
Revises: 040

공저자 페이지(함께 작성한 논문)에서 저자·공저자가 각 논문의 몇 저자인지 보여주려고, 논문마다
KCI articleDetail의 저자 역할 두 속성을 그대로 적재한다.
  author-division  '1' 제1저자 / '2' 그 외 — 표본 200편 전부 보유
  author-part      제1 | 교신 | 참여 | 단독 — 2015년 이전 논문은 27%가 비어 온다(2026-11-06 실측)

researcher_external_papers(KCI 이력)는 articleSearch로 받아 역할이 없고, researcher_papers.role은
코퍼스 논문 3,026쌍에만 있다. 공저 논문 20,367편 중 KCI ID가 있는 20,299편이 적재 대상이다
(scripts/load_kci_author_roles.py).

배열 네 개는 같은 길이·같은 순서(논문에 적힌 저자 순). 저자가 0명인 응답도 빈 배열로 남겨 다시 부르지 않는다.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kci_article_author_roles",
        sa.Column("art_id", sa.String(20), primary_key=True),
        sa.Column("author_names", postgresql.ARRAY(sa.String(300)), nullable=False),
        sa.Column("author_institutions", postgresql.ARRAY(sa.String(500)), nullable=False),
        sa.Column("author_divisions", postgresql.ARRAY(sa.String(2)), nullable=False),
        sa.Column("author_parts", postgresql.ARRAY(sa.String(10)), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("kci_article_author_roles")
