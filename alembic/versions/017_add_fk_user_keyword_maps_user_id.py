"""add FK user_keyword_maps.user_id → users.id

Revision ID: 017
Revises: 016
Create Date: 2026-06-03 00:00:00.000000
"""

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 고아 행 제거 (FK 추가 전 정합성 보장)
    op.execute("""
        DELETE FROM user_keyword_maps
        WHERE user_id NOT IN (SELECT id FROM users)
    """)

    op.create_foreign_key(
        "fk_user_keyword_maps_user_id",
        "user_keyword_maps",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_user_keyword_maps_user_id",
        "user_keyword_maps",
        type_="foreignkey",
    )
