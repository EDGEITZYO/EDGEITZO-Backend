"""create recent_reads table

Revision ID: 006
Revises: 005
Create Date: 2026-05-20 13:10:04.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recent_reads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("paper_id", sa.String(length=100), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recent_reads_user_read_at", "recent_reads", ["user_id", "read_at"], unique=False)
    op.create_index("ix_recent_reads_user_deleted_at", "recent_reads", ["user_id", "deleted_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_recent_reads_user_deleted_at", table_name="recent_reads")
    op.drop_index("ix_recent_reads_user_read_at", table_name="recent_reads")
    op.drop_table("recent_reads")
