"""researcher schema v2 — 식별자 체계 교체 + 관계 속성 + 외부 논문 테이블

Revision ID: 026
Revises: 025
Create Date: 2026-08-22

`researchers.cn`은 ScienceON 연구자 CN 전용이었으나, ScienceON의 연구자↔논문 색인이
불완전해(실측 매칭률 46%) KCI 파생 식별자를 1급으로 올린다. PK를 출처 접두어가 붙은
`researcher_id`로 바꾸고, 기존 189행은 `sci:<cn>`으로 재키잉해 그대로 보존한다.

    kci:<sha1(정규화이름|기관루트)[:16]>   KCI articleDetail 파생 (기본)
    oa:<openalex author id>              OpenAlex (해외 학술지 저자)
    sci:<scienceon cn>                   ScienceON에서만 확인된 연구자
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── researchers: PK 재키잉 (데이터 보존) ──────────────────────────────
    op.add_column("researchers", sa.Column("researcher_id", sa.String(length=100), nullable=True))
    op.execute("UPDATE researchers SET researcher_id = 'sci:' || cn")

    op.add_column("researcher_papers", sa.Column("researcher_id", sa.String(length=100), nullable=True))
    op.execute("UPDATE researcher_papers SET researcher_id = 'sci:' || researcher_cn")

    op.drop_constraint("researcher_papers_researcher_cn_fkey", "researcher_papers", type_="foreignkey")
    op.execute("ALTER TABLE researcher_papers DROP CONSTRAINT researcher_papers_pkey")
    op.execute("ALTER TABLE researchers DROP CONSTRAINT researchers_pkey")

    # cn은 ScienceON CN 보관용 컬럼으로 격하
    op.alter_column("researchers", "cn", new_column_name="scienceon_cn", existing_type=sa.String(length=100), nullable=True)
    op.drop_column("researcher_papers", "researcher_cn")

    op.alter_column("researchers", "researcher_id", nullable=False)
    op.alter_column("researcher_papers", "researcher_id", nullable=False)
    op.create_primary_key("researchers_pkey", "researchers", ["researcher_id"])
    op.create_primary_key("researcher_papers_pkey", "researcher_papers", ["researcher_id", "paper_id"])
    op.create_foreign_key(
        "researcher_papers_researcher_id_fkey", "researcher_papers", "researchers",
        ["researcher_id"], ["researcher_id"], ondelete="CASCADE",
    )
    op.create_index("ix_researchers_scienceon_cn", "researchers", ["scienceon_cn"])

    # ── researchers: 신규 컬럼 ────────────────────────────────────────────
    op.add_column("researchers", sa.Column("source", sa.String(length=20), nullable=True))
    op.execute("UPDATE researchers SET source = 'scienceon'")
    op.alter_column("researchers", "source", nullable=False)

    for col in (
        sa.Column("institution_current", sa.String(length=500), nullable=True),
        sa.Column("institution_dept", sa.String(length=500), nullable=True),
        sa.Column("institution_history", postgresql.ARRAY(sa.String(length=500)), nullable=True),
        # 이메일 신뢰도 — ScienceON 연구자 색인이 불완전해 소속만으로 추정한 건이 섞인다.
        # 'confirmed' = 논문 CN 교집합까지 확인, 'inferred' = 이름+소속 유일 일치, NULL = 이메일 없음
        sa.Column("match_confidence", sa.String(length=20), nullable=True),
        sa.Column("keywords", postgresql.ARRAY(sa.String(length=200)), nullable=True),
        sa.Column("total_papers", sa.Integer(), nullable=True),
        sa.Column("total_citations", sa.Integer(), nullable=True),
        sa.Column("citation_source", sa.String(length=20), nullable=True),  # 'kci' | 'openalex'
        sa.Column("corpus_paper_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_pubyear", sa.Integer(), nullable=True),
        sa.Column("last_pubyear", sa.Integer(), nullable=True),
        sa.Column("papers_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("expanded_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("researchers", col)

    op.execute("UPDATE researchers SET institution_current = author_inst_kor, keywords = keyword, total_papers = article_cnt")
    op.create_index("ix_researchers_name_ko", "researchers", ["author_name_kor"])
    op.create_index("ix_researchers_institution_current", "researchers", ["institution_current"])

    # ── researcher_papers: 관계 속성 ──────────────────────────────────────
    op.add_column("researcher_papers", sa.Column("author_order", sa.Integer(), nullable=True))
    op.add_column("researcher_papers", sa.Column("role", sa.String(length=20), nullable=True))  # 제1|교신|참여|단독
    op.add_column("researcher_papers", sa.Column("institution_at_time", sa.String(length=500), nullable=True))

    # ── researcher_external_papers ────────────────────────────────────────
    op.create_table(
        "researcher_external_papers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("researcher_id", sa.String(length=100), nullable=False),
        sa.Column("external_source", sa.String(length=20), nullable=False),  # 'kci' | 'openalex'
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("journal", sa.String(length=500), nullable=True),
        sa.Column("pubyear", sa.Integer(), nullable=True),
        sa.Column("pubmonth", sa.String(length=2), nullable=True),
        sa.Column("citation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("categories", postgresql.ARRAY(sa.String(length=200)), nullable=True),
        sa.Column("keywords", postgresql.ARRAY(sa.String(length=200)), nullable=True),
        sa.Column("doi", sa.String(length=200), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("internal_paper_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["researcher_id"], ["researchers.researcher_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["internal_paper_id"], ["papers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("researcher_id", "external_id", name="uq_researcher_external_papers_rid_extid"),
    )
    op.create_index("ix_researcher_external_papers_rid_year", "researcher_external_papers", ["researcher_id", "pubyear"])


def downgrade() -> None:
    op.drop_table("researcher_external_papers")
    for c in ("institution_at_time", "role", "author_order"):
        op.drop_column("researcher_papers", c)

    op.drop_index("ix_researchers_institution_current", table_name="researchers")
    op.drop_index("ix_researchers_name_ko", table_name="researchers")
    for c in ("expanded_at", "papers_truncated", "last_pubyear", "first_pubyear", "corpus_paper_count",
              "citation_source", "total_citations", "total_papers", "keywords", "match_confidence",
              "institution_history", "institution_dept", "institution_current", "source"):
        op.drop_column("researchers", c)

    op.drop_index("ix_researchers_scienceon_cn", table_name="researchers")
    op.add_column("researcher_papers", sa.Column("researcher_cn", sa.String(length=100), nullable=True))
    op.execute("UPDATE researcher_papers SET researcher_cn = split_part(researcher_id, ':', 2)")
    op.drop_constraint("researcher_papers_researcher_id_fkey", "researcher_papers", type_="foreignkey")
    op.execute("ALTER TABLE researcher_papers DROP CONSTRAINT researcher_papers_pkey")
    op.execute("ALTER TABLE researchers DROP CONSTRAINT researchers_pkey")
    op.drop_column("researcher_papers", "researcher_id")
    op.drop_column("researchers", "researcher_id")
    op.alter_column("researchers", "scienceon_cn", new_column_name="cn", existing_type=sa.String(length=100), nullable=False)
    op.create_primary_key("researchers_pkey", "researchers", ["cn"])
    op.create_primary_key("researcher_papers_pkey", "researcher_papers", ["researcher_cn", "paper_id"])
    op.create_foreign_key("researcher_papers_researcher_cn_fkey", "researcher_papers", "researchers",
                          ["researcher_cn"], ["cn"], ondelete="CASCADE")
