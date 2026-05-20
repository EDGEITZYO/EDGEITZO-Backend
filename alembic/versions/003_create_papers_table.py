"""create papers table

Revision ID: 003
Revises: 002
Create Date: 2026-05-20 13:10:01.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "papers",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("scienceon_cn", sa.String(length=100), nullable=True),
        sa.Column("semantic_scholar_id", sa.String(length=100), nullable=True),
        sa.Column("doi", sa.String(length=200), nullable=True),
        sa.Column("issn", sa.String(length=20), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("title_en", sa.String(length=1000), nullable=True),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("abstract_en", sa.Text(), nullable=True),
        sa.Column("authors", postgresql.ARRAY(sa.String(length=300)), nullable=True),
        sa.Column("keywords_ko", postgresql.ARRAY(sa.String(length=200)), nullable=True),
        sa.Column("keywords_en", postgresql.ARRAY(sa.String(length=200)), nullable=True),
        sa.Column("pubyear", sa.Integer(), nullable=True),
        sa.Column("paper_type", sa.String(length=50), nullable=True),
        sa.Column("citation_count", sa.Integer(), nullable=False),
        sa.Column("journal_id", sa.Integer(), nullable=True),
        sa.Column("db_code", sa.String(length=20), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["journal_id"], ["journals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_papers_doi", "papers", ["doi"], unique=True)
    op.create_index("ix_papers_issn", "papers", ["issn"], unique=False)
    op.create_index("ix_papers_pubyear", "papers", ["pubyear"], unique=False)
    op.create_index("ix_papers_scienceon_cn", "papers", ["scienceon_cn"], unique=True)
    op.create_index("ix_papers_semantic_scholar_id", "papers", ["semantic_scholar_id"], unique=True)
    op.create_index("ix_papers_source_pubyear", "papers", ["source", "pubyear"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_papers_source_pubyear", table_name="papers")
    op.drop_index("ix_papers_semantic_scholar_id", table_name="papers")
    op.drop_index("ix_papers_scienceon_cn", table_name="papers")
    op.drop_index("ix_papers_pubyear", table_name="papers")
    op.drop_index("ix_papers_issn", table_name="papers")
    op.drop_index("ix_papers_doi", table_name="papers")
    op.drop_table("papers")
