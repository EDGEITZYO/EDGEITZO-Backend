"""create researcher_flow_cache

Revision ID: 032
Revises: 031
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "researcher_flow_cache",
        sa.Column("researcher_id", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("paper_signature", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary_source", sa.String(length=10), nullable=False),
        sa.Column("model", sa.String(length=50), nullable=True),
        sa.Column("paper_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("researcher_id", "prompt_version"),
    )
    # 예산이 복구된 뒤 rule 폴백으로 만들어진 것만 골라 다시 돌리기 위한 인덱스
    op.create_index(
        "ix_researcher_flow_cache_source", "researcher_flow_cache", ["summary_source"]
    )


def downgrade() -> None:
    op.drop_index("ix_researcher_flow_cache_source", table_name="researcher_flow_cache")
    op.drop_table("researcher_flow_cache")
