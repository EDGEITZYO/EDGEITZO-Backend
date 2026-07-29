"""add journal_name to papers

Revision ID: 023
Revises: 022
Create Date: 2026-07-29
"""
import sqlalchemy as sa
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("journal_name", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "journal_name")
