"""create researchers table

Revision ID: 021
Revises: 020
Create Date: 2026-07-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "researchers",
        sa.Column("cn", sa.String(length=100), primary_key=True),
        sa.Column("author_name_kor", sa.String(length=300), nullable=True),
        sa.Column("author_name_eng", sa.String(length=300), nullable=True),
        sa.Column("author_inst_kor", sa.String(length=500), nullable=True),
        sa.Column("author_inst_eng", sa.String(length=500), nullable=True),
        sa.Column("rno", sa.String(length=100), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("keyword", postgresql.ARRAY(sa.String(length=200)), nullable=True),
        sa.Column("article_cnt", sa.Integer(), nullable=True),
        sa.Column("patent_cnt", sa.Integer(), nullable=True),
        sa.Column("report_cnt", sa.Integer(), nullable=True),
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
    )


def downgrade() -> None:
    op.drop_table("researchers")
