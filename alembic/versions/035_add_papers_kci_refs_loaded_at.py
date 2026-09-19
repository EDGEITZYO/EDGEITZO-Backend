"""add papers.kci_refs_loaded_at

Revision ID: 035
Revises: 034

KCI 참고문헌을 받아 이 환경의 paper_citation_external_refs에 넣었는지 표시한다.
처음엔 이 표시를 Neo4j 노드(refs_loaded_at)에 뒀는데, Neo4j Aura는 로컬·운영이 같은 인스턴스를
쓰고 참고문헌 행은 환경별 Postgres에 들어간다. 한 환경에서 받으면 다른 환경은 "이미 받았다"고
보고 건너뛰어, 그 환경 그래프에서 해외 참고문헌이 빠졌다. 표시는 행과 같은 곳에 둔다.
NULL이면 아직 안 받은 것 — 상세·그래프·확장 요청 때 받는다.
"""
import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("kci_refs_loaded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "kci_refs_loaded_at")
