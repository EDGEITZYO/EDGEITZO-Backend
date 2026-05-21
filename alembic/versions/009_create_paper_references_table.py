"""create paper_references table

Revision ID: 009
Revises: 008
Create Date: 2026-05-22 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_references",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_cn", sa.String(length=100), nullable=False),
        sa.Column("cited_cn", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("author", sa.String(length=500), nullable=True),
        sa.Column("journal", sa.String(length=500), nullable=True),
        sa.Column("doi", sa.String(length=200), nullable=True),
        sa.Column("pubyear", sa.Integer(), nullable=True),
        sa.Column("issn", sa.String(length=50), nullable=True),
        sa.Column("material_type", sa.String(length=50), nullable=True),
        # internal_paper_id: cited_cn 또는 doi 기준으로 papers.id 와 매칭되면 채움
        sa.Column("internal_paper_id", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["source_cn"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["internal_paper_id"], ["papers.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_references_source_cn", "paper_references", ["source_cn"])
    op.create_index("ix_paper_references_cited_cn", "paper_references", ["cited_cn"])
    op.create_index(
        "ix_paper_references_internal_paper_id",
        "paper_references",
        ["internal_paper_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paper_references_internal_paper_id", table_name="paper_references"
    )
    op.drop_index("ix_paper_references_cited_cn", table_name="paper_references")
    op.drop_index("ix_paper_references_source_cn", table_name="paper_references")
    op.drop_table("paper_references")
