"""add birth_year to users

Revision ID: 024
Revises: 023
Create Date: 2026-08-01
"""
import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("birth_year", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "birth_year")
