"""add sjr_year to journals

Revision ID: 007
Revises: 006
Create Date: 2026-05-20 14:20:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("journals", sa.Column("sjr_year", sa.Integer(), nullable=True))
    op.create_index("ix_journals_sjr_year", "journals", ["sjr_year"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_journals_sjr_year", table_name="journals")
    op.drop_column("journals", "sjr_year")
