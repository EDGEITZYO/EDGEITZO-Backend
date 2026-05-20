"""create bookmarks table

Revision ID: 005
Revises: 004
Create Date: 2026-05-20 13:10:03.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bookmarks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("paper_id", sa.String(length=100), nullable=False),
        sa.Column("folder_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["folder_id"], ["bookmark_folders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "paper_id", name="uq_bookmarks_user_paper"),
    )
    op.create_index("ix_bookmarks_user_folder", "bookmarks", ["user_id", "folder_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_bookmarks_user_folder", table_name="bookmarks")
    op.drop_table("bookmarks")
