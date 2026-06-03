"""add kci_art_id to papers

Revision ID: 016
Revises: 015
Create Date: 2026-06-03 00:00:00.000000
"""

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS kci_art_id VARCHAR(50)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_papers_kci_art_id ON papers (kci_art_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_papers_kci_art_id")
    op.execute("ALTER TABLE papers DROP COLUMN IF EXISTS kci_art_id")
