"""add normalized title index on journals

Revision ID: 033
Revises: 032

코퍼스 밖 논문에는 ISSN이 없어 journal_id를 못 붙인다. 그래서 학술지명으로 잇는데
(lower(btrim(title)) 비교), 인덱스가 없으면 논문 한 편마다 journals 7,475행을 훑는다.
341편 연구자의 논문 목록이 1.1초까지 늘어났다.
"""
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_journals_title_norm ON journals (lower(btrim(title)))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_journals_title_norm")
