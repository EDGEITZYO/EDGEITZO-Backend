"""add research_field to users

Revision ID: 015
Revises: 014
Create Date: 2026-06-03 00:00:00.000000
"""

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS research_field VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS research_field")
