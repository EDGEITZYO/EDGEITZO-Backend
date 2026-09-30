"""add researcher_paper_exclusions + researchers.kci_cret_id

Revision ID: 039
Revises: 038

KCI 저자 번호로 '같은 이름의 다른 사람 논문'이라고 판정해 뺀 연구자-논문 쌍을 남긴다.
정리 스크립트(reconcile_researcher_papers.py)는 소속이 맞으면 논문을 붙이므로, 기록 없이 다시 돌리면
같은 기관 동명이인의 논문이 되돌아온다. 이 표에 있는 쌍은 다시 붙이지 않는다.

researchers.kci_cret_id: 그 연구자로 판정한 KCI 저자 번호(CRT…). 판정 불가면 null.
"""
import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "researcher_paper_exclusions",
        sa.Column(
            "researcher_id",
            sa.String(100),
            sa.ForeignKey("researchers.researcher_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("external_id", sa.String(100), primary_key=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("researchers", sa.Column("kci_cret_id", sa.String(20), nullable=True))
    op.create_index("ix_researchers_kci_cret_id", "researchers", ["kci_cret_id"])


def downgrade() -> None:
    op.drop_index("ix_researchers_kci_cret_id", table_name="researchers")
    op.drop_column("researchers", "kci_cret_id")
    op.drop_table("researcher_paper_exclusions")
