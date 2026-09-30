"""add kci_article_authors / kci_authors / kci_author_fetch_log

Revision ID: 038
Revises: 037

KCI가 저자마다 붙이는 연구자 번호(CRT…)를 적재한다. 공공데이터포털 "한국연구재단_KCI 논문정보서비스"의
두 오퍼레이션에서 받는다 (키: KCI_DATA_GO_KR_KEY).
  KCI논문저자 조회 (openApiD311List, artiId)  → 논문별 저자 번호·소속
  저자 정보 조회   (openApiM330List, certNm)  → 이름별 저자 번호·소속·영문명·KRI ID

왜 필요한가: KCI Open API(articleSearch/articleDetail)는 저자 이름·소속 문자열만 준다.
같은 기관의 동명이인은 그걸로 가를 수 없다 — "김민정"만 해도 KCI 저자 번호가 1,467개다
(2026-09-29 실측). 저자 번호가 있으면 한 사람의 논문 = 한 번호로 정확히 묶인다.

조회 한도(개발 계정 하루 5,000건)가 있어 여러 날에 나눠 받는다. 한 번 받은 논문·이름은 다시
부르지 않도록 kci_author_fetch_log에 남긴다(저자가 0명인 응답도 '받음'으로 기록).
"""
import sqlalchemy as sa
from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kci_article_authors",
        sa.Column("arti_cret_id", sa.String(30), primary_key=True),  # 논문저자 ID — 논문×저자 한 칸
        sa.Column("arti_id", sa.String(20), nullable=False),
        sa.Column("cret_id", sa.String(20), nullable=True),  # 저자 번호. KCI가 식별 못 한 저자는 비어 있다
        sa.Column("cret_div_cd", sa.String(7), nullable=True),  # 01 제1저자 / 02 그 외
        sa.Column("belo_insi_id", sa.String(20), nullable=True),
        sa.Column("belo_insi_nm", sa.String(500), nullable=True),
        sa.Column("kri_part_div_cd", sa.String(2), nullable=True),
        sa.Column("orcid", sa.String(40), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_kci_article_authors_arti_id", "kci_article_authors", ["arti_id"])
    op.create_index("ix_kci_article_authors_cret_id", "kci_article_authors", ["cret_id"])

    op.create_table(
        "kci_authors",
        sa.Column("cret_id", sa.String(20), primary_key=True),
        sa.Column("kri_id", sa.String(30), nullable=True),
        sa.Column("kor_nm", sa.String(150), nullable=True),
        sa.Column("eng_nm", sa.String(300), nullable=True),
        sa.Column("belo_insi_id", sa.String(20), nullable=True),
        sa.Column("belo_insi_nm", sa.String(500), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_kci_authors_kor_nm", "kci_authors", ["kor_nm"])

    op.create_table(
        "kci_author_fetch_log",
        sa.Column("kind", sa.String(10), primary_key=True),  # 'article' | 'name'
        sa.Column("key", sa.String(150), primary_key=True),  # 논문 ID 또는 저자명
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("kci_author_fetch_log")
    op.drop_index("ix_kci_authors_kor_nm", table_name="kci_authors")
    op.drop_table("kci_authors")
    op.drop_index("ix_kci_article_authors_cret_id", table_name="kci_article_authors")
    op.drop_index("ix_kci_article_authors_arti_id", table_name="kci_article_authors")
    op.drop_table("kci_article_authors")
