"""create journals table

Revision ID: 002
Revises: 001
Create Date: 2026-05-20 13:10:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sjr_sourceid", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("issn", postgresql.ARRAY(sa.String(length=20)), nullable=True),
        sa.Column("type", sa.String(length=50), nullable=True),
        sa.Column("sjr_score", sa.Float(), nullable=True),
        sa.Column("sjr_best_quartile", sa.String(length=2), nullable=True),
        sa.Column("h_index", sa.Integer(), nullable=True),
        sa.Column("sci_indexed", sa.Boolean(), nullable=False),
        sa.Column("kci_indexed", sa.Boolean(), nullable=False),
        sa.Column("if_value", sa.Float(), nullable=True),
        sa.Column("categories", postgresql.ARRAY(sa.String(length=200)), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("coverage", sa.String(length=100), nullable=True),
        sa.Column("publisher", sa.String(length=300), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_journals_issn_gin", "journals", ["issn"], unique=False, postgresql_using="gin")
    op.create_index("ix_journals_sjr_sourceid", "journals", ["sjr_sourceid"], unique=True)
    op.create_index("ix_journals_title", "journals", ["title"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_journals_title", table_name="journals")
    op.drop_index("ix_journals_sjr_sourceid", table_name="journals")
    op.drop_index("ix_journals_issn_gin", table_name="journals", postgresql_using="gin")
    op.drop_table("journals")
