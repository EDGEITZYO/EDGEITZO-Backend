"""외부 논문에 논문 유형·서지 지표 추가

Revision ID: 029
Revises: 028
Create Date: 2026-08-23

명세서 「연도별 논문 이력」의 각 항목에 '논문 유형'이 있는데 외부 논문에는 컬럼이 없었다.
paper_type은 API 필드가 아니라 출처에서 파생하는 값이다(papers 쪽도 DBCode로 분류했다).
KCI articleSearch로 모은 논문은 전부 등재지 학술논문이므로 'journal'로 확정된다.

fwci는 KCI articleDetail이 주는 분야보정 피인용 지표(Field-Weighted Citation Impact)다.
피인용 원값은 KCI 중앙값 20 vs OpenAlex 709로 35배 차이라 한 척도에 못 올리는데,
fwci는 분야 평균을 1.0으로 맞춘 값이라 출처가 달라도 비교할 여지가 있다.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("researcher_external_papers", sa.Column("paper_type", sa.String(length=50), nullable=True))
    op.add_column("researcher_external_papers", sa.Column("fwci", sa.Float(), nullable=True))
    op.add_column("researcher_external_papers", sa.Column("language", sa.String(length=30), nullable=True))
    # 'Y'/'N' — 정규 논문 여부
    op.add_column("researcher_external_papers", sa.Column("regularity", sa.String(length=5), nullable=True))
    # 등재 / 등재후보 / 우수등재
    op.add_column("researcher_external_papers", sa.Column("kci_registration", sa.String(length=20), nullable=True))

    # KCI articleSearch에서 온 논문은 정의상 등재지 학술논문이다.
    op.execute("UPDATE researcher_external_papers SET paper_type = 'journal' WHERE external_source = 'kci'")


def downgrade() -> None:
    for col in ("kci_registration", "regularity", "language", "fwci", "paper_type"):
        op.drop_column("researcher_external_papers", col)
