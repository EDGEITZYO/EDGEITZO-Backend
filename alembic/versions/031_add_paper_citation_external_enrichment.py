"""코퍼스 밖 참고문헌의 상세 정보 사전 적재용 컬럼

Revision ID: 031
Revises: 030
Create Date: 2026-08-31

paper_citation_external_refs는 지금까지 KCI/OpenAlex가 준 서지정보(제목/저자/저널/연도/DOI)만
담고 있었고, 초록·링크는 노드를 클릭한 시점에 외부 API로 매번 받아왔다. 그 방식의 문제가 둘이다.

1. 지연 — 클릭마다 외부 호출이 나간다(실측 KCI 114ms / OpenAlex 297ms, p90 354ms).
   24시간 Redis 캐시로 가리고 있었지만 첫 클릭과 캐시 만료 후는 그대로 노출된다.
2. 커버리지 — 참고문헌의 66.9%가 DOI도 KCI arti-id도 없어 조회할 수단 자체가 없었다.

여기서 컬럼을 추가해 사전 적재로 돌린다. 행은 늘지 않는다(기존 18,093행에 값만 채움).

DOI 없는 건은 Crossref query.bibliographic으로 DOI를 역으로 찾아낸다 — 우리에게 제목뿐
아니라 저널·연도·제1저자가 있어서 가능하다(실측 매칭률 71.7%). 그렇게 얻은 DOI는 출처가
다르므로 KCI가 준 doi 컬럼에 섞지 않고 resolved_doi에 따로 둔다. 매칭은 확률적이라
나중에 오매칭이 발견됐을 때 어느 쪽이 추정값인지 구분할 수 있어야 한다.

초록은 OpenAlex를 1순위로 쓴다. Semantic Scholar는 매칭에는 좋지만 초록이 법적 제약으로
막혀 있어(실측 3.3%) OpenAlex(43~70%)와 차이가 크다. S2는 초록이 없을 때 tldr 폴백으로만 쓴다.
tldr은 사람이 쓴 초록이 아니라 AllenAI 모델이 생성한 요약이므로 abstract_source로 구분해
저장하고, 화면에서도 초록과 다르게 표시해야 한다.

enrich_status를 두는 이유: 실패한 건을 재시도할지 판단하려면 "아직 안 해봤다"와 "해봤는데
못 찾았다"를 구분해야 한다. 매칭 안 되는 건의 상당수는 EU 법령·정부보고서·표준처럼 애초에
논문이 아니라서(어느 API에도 초록이 없다) 재시도해도 결과가 같다.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # nullable + server_default 없음 → 테이블 재작성 없이 메타데이터만 변경(즉시 완료).
    # 기존 18,093행의 데이터는 그대로 두고 백필 스크립트가 나중에 채운다.
    op.add_column("paper_citation_external_refs", sa.Column("abstract", sa.Text(), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("abstract_lang", sa.String(length=2), nullable=True))
    # 'kci' | 'openalex' | 's2' | 's2_tldr' — s2_tldr만 AI 생성 요약이라 화면 표기를 달리한다
    op.add_column("paper_citation_external_refs", sa.Column("abstract_source", sa.String(length=20), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("title_en", sa.String(length=1000), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("keywords", postgresql.ARRAY(sa.String(length=200)), nullable=True))
    # KCI가 준 doi와 구분한다 — 이쪽은 Crossref 제목유사도 매칭으로 얻은 추정값
    op.add_column("paper_citation_external_refs", sa.Column("resolved_doi", sa.String(length=200), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("external_url", sa.String(length=1000), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("pdf_url", sa.String(length=1000), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("citation_count", sa.Integer(), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("publisher", sa.String(length=500), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("issn", sa.String(length=50), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("is_open_access", sa.Boolean(), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("kci_registered", sa.Boolean(), nullable=True))
    # 'ok' | 'no_abstract' | 'no_match' | 'error' — null이면 아직 시도 안 한 행
    op.add_column("paper_citation_external_refs", sa.Column("enrich_status", sa.String(length=20), nullable=True))
    op.add_column("paper_citation_external_refs", sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))

    # 노드 클릭 시 상세 조회가 external_id 단건 조회인데 인덱스가 없어 18,093행 Seq Scan을
    # 돌고 있었다(실측 5.4ms). 이 테이블에서 유일하게 행 수에 비례해 나빠지던 쿼리다.
    op.create_index(
        "ix_paper_citation_external_refs_external_id",
        "paper_citation_external_refs",
        ["external_id"],
    )
    # 백필 스크립트가 "아직 안 한 행"을 골라올 때 쓴다. 부분 인덱스라 작다.
    op.create_index(
        "ix_paper_citation_external_refs_enrich_pending",
        "paper_citation_external_refs",
        ["enrich_status"],
        postgresql_where=sa.text("enrich_status IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_paper_citation_external_refs_enrich_pending", table_name="paper_citation_external_refs")
    op.drop_index("ix_paper_citation_external_refs_external_id", table_name="paper_citation_external_refs")
    for column in (
        "enriched_at", "enrich_status", "kci_registered", "is_open_access", "issn", "publisher",
        "citation_count", "pdf_url", "external_url", "resolved_doi", "keywords", "title_en",
        "abstract_source", "abstract_lang", "abstract",
    ):
        op.drop_column("paper_citation_external_refs", column)
