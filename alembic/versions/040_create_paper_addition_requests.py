"""create paper_addition_requests

Revision ID: 040
Revises: 039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_addition_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "external_id",
            name="uq_paper_addition_requests_user_external",
        ),
    )
    op.create_index(
        "ix_paper_addition_requests_external_id",
        "paper_addition_requests",
        ["external_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_paper_addition_requests_external_id",
        table_name="paper_addition_requests",
    )
    op.drop_table("paper_addition_requests")
