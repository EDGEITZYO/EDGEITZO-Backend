"""add is_guest to users + guest nickname sequence

Revision ID: 034
Revises: 033

데모(심사·투표) 기간에 로그인 없이 체험하는 게스트 계정 구분용. 게스트도 users 행을 그대로
쓰므로 북마크·최근 열람·탐색 이력 등 user_id에 묶인 기능이 수정 없이 동작한다.

email NOT NULL/UNIQUE는 풀지 않는다 — 게스트는 guest_{uuid}@guest.local 플레이스홀더를
넣는다. 기존 코드가 email을 non-null로 가정하는 곳을 건드리지 않기 위해서다.

닉네임("게스트{n}")의 번호는 시퀀스로 뽑는다. 동시 발급에도 겹치지 않는다.
"""
import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_users_is_guest", "users", ["is_guest"])
    op.execute("CREATE SEQUENCE IF NOT EXISTS guest_nickname_seq START 1")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS guest_nickname_seq")
    op.drop_index("ix_users_is_guest", table_name="users")
    op.drop_column("users", "is_guest")
