"""create paper_similar table

Revision ID: 008
Revises: 007
Create Date: 2026-05-22 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_similar",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_cn", sa.String(length=100), nullable=False),
        sa.Column("similar_cn", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("author", sa.String(length=500), nullable=True),
        sa.Column("pubyear", sa.Integer(), nullable=True),
        sa.Column("issn", sa.String(length=50), nullable=True),
        sa.Column("material_type", sa.String(length=50), nullable=True),
        # internal_paper_id: similar_cn이 papers.id 와 매칭되면 채움
        sa.Column("internal_paper_id", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["source_cn"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["internal_paper_id"], ["papers.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_similar_source_cn", "paper_similar", ["source_cn"])
    op.create_index("ix_paper_similar_similar_cn", "paper_similar", ["similar_cn"])
    op.create_index(
        "ix_paper_similar_internal_paper_id",
        "paper_similar",
        ["internal_paper_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_paper_similar_internal_paper_id", table_name="paper_similar")
    op.drop_index("ix_paper_similar_similar_cn", table_name="paper_similar")
    op.drop_index("ix_paper_similar_source_cn", table_name="paper_similar")
    op.drop_table("paper_similar")
