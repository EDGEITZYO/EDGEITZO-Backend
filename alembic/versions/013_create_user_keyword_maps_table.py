"""create user_keyword_maps table

Revision ID: 013
Revises: 012
Create Date: 2026-06-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_keyword_maps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("research_field", sa.String(), nullable=False),
        sa.Column("tree", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", name="uq_user_keyword_maps_user_id"),
    )
    op.create_index("ix_user_keyword_maps_user_id", "user_keyword_maps", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_keyword_maps_user_id", table_name="user_keyword_maps")
    op.drop_table("user_keyword_maps")
