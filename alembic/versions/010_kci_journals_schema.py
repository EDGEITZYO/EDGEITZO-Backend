"""KCI 저널 스키마 보강

- sjr_sourceid nullable 변경 (KCI 전용 저널 upsert 허용)
- p_issn, e_issn 컬럼 추가 (부분 유니크 인덱스)
- kci_status 컬럼 추가
- SJR 기존 row: issn ARRAY 첫 번째 → p_issn, 두 번째 → e_issn 역추정 백필

Revision ID: 010
Revises: 009
Create Date: 2026-05-26
"""

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("journals", "sjr_sourceid", nullable=True)

    op.add_column("journals", sa.Column("p_issn", sa.String(9), nullable=True))
    op.add_column("journals", sa.Column("e_issn", sa.String(9), nullable=True))
    op.add_column("journals", sa.Column("kci_status", sa.String(20), nullable=True))

    op.create_index(
        "ix_journals_p_issn",
        "journals",
        ["p_issn"],
        unique=True,
        postgresql_where=sa.text("p_issn IS NOT NULL"),
    )
    op.create_index(
        "ix_journals_e_issn",
        "journals",
        ["e_issn"],
        unique=True,
        postgresql_where=sa.text("e_issn IS NOT NULL"),
    )

    # SJR 기존 row 백필: issn[1] → p_issn, issn[2] → e_issn
    op.execute(sa.text("""
        UPDATE journals
        SET
            p_issn = CASE
                WHEN array_length(issn, 1) >= 1
                THEN SUBSTRING(issn[1], 1, 4) || '-' || SUBSTRING(issn[1], 5, 4)
                ELSE NULL
            END,
            e_issn = CASE
                WHEN array_length(issn, 1) >= 2
                THEN SUBSTRING(issn[2], 1, 4) || '-' || SUBSTRING(issn[2], 5, 4)
                ELSE NULL
            END
        WHERE issn IS NOT NULL AND array_length(issn, 1) >= 1
    """))


def downgrade() -> None:
    op.drop_index("ix_journals_e_issn", table_name="journals")
    op.drop_index("ix_journals_p_issn", table_name="journals")
    op.drop_column("journals", "kci_status")
    op.drop_column("journals", "e_issn")
    op.drop_column("journals", "p_issn")
    op.alter_column("journals", "sjr_sourceid", nullable=False)
