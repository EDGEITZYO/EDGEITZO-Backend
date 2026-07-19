"""create researcher_papers table

Revision ID: 022
Revises: 021
Create Date: 2026-07-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "researcher_papers",
        sa.Column("researcher_cn", sa.String(length=100), sa.ForeignKey("researchers.cn", ondelete="CASCADE"), primary_key=True),
        sa.Column("paper_id", sa.String(length=100), sa.ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_researcher_papers_paper_id", "researcher_papers", ["paper_id"])


def downgrade() -> None:
    op.drop_index("ix_researcher_papers_paper_id", table_name="researcher_papers")
    op.drop_table("researcher_papers")
