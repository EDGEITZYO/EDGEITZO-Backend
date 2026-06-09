"""alter paper_similar author to text

Revision ID: 018
Revises: 017
Create Date: 2026-06-09
"""
from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "paper_similar",
        "author",
        type_=sa.Text(),
        existing_type=sa.String(length=500),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "paper_similar",
        "author",
        type_=sa.String(length=500),
        existing_type=sa.Text(),
        existing_nullable=True,
    )
