"""create paper_citation_external_refs table

Revision ID: 025
Revises: 024
Create Date: 2026-08-13
"""
import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_citation_external_refs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_cn", sa.String(length=100), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("external_source", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("authors", sa.ARRAY(sa.String(length=300)), nullable=True),
        sa.Column("journal", sa.String(length=500), nullable=True),
        sa.Column("doi", sa.String(length=200), nullable=True),
        sa.Column("pubyear", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_cn"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_cn", "direction", "external_id", name="uq_paper_citation_external_refs_source_dir_extid"),
    )
    op.create_index(
        "ix_paper_citation_external_refs_source_direction",
        "paper_citation_external_refs",
        ["source_cn", "direction"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paper_citation_external_refs_source_direction",
        table_name="paper_citation_external_refs",
    )
    op.drop_table("paper_citation_external_refs")
