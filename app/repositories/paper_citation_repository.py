from __future__ import annotations

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper, PaperCitationExternalRef


def _not_in_corpus():
    """같은 논문이 코퍼스에 다른 ID로 이미 있으면 제외하는 조건.

    코퍼스 논문은 id가 ScienceON CN(JAKO…/NART…)이고 KCI ID는 kci_art_id에 따로 있다.
    참고문헌 행의 external_id가 그 kci_art_id와 같으면 같은 논문인데 key가 달라, 그대로 두면
    같은 논문이 CN 노드와 ART 노드로 두 번 나온다. 이런 행은 국내 논문 적재
    (app/services/domestic_paper_service.py) 때 CITES로 옮겨지지만, 옮기기 전 상태를 대비해
    조회 시점에도 걸러 둔다.

    id 자체가 external_id인 논문(연구자 논문 편입분 등)은 걸러내지 않는다 — key가 같아
    화면 중복 제거(excluded_ids)로 충분하고, 걸러내면 그 참고문헌이 그래프에서 사라진다."""
    return ~(
        select(Paper.id)
        .where(
            Paper.kci_art_id == PaperCitationExternalRef.external_id,
            Paper.id != PaperCitationExternalRef.external_id,
        )
        .exists()
    )


def _displayable():
    """그래프에 올릴 참고문헌만 남긴다.

    - KCI 논문 ID(ART…)가 있으면 국내 논문 — 항상 표시 (in_service, 누르면 적재)
    - 그 외는 해외 논문 — 제목이 있고 한글이 아닌 것만 표시
    KCI ID가 없는데 제목이 한글인 항목은 단행본·법령·백서·기사 같은 KCI 밖 국내 자료다(실측 표본).
    국내 논문도 해외 논문도 아니고 상세 정보를 구할 곳도 없어 그래프에서 뺀다.
    제목 없는 항목은 노드로 표시할 수 없어 뺀다."""
    ref = PaperCitationExternalRef
    return or_(
        ref.external_id.like("ART%"),
        and_(ref.title.isnot(None), ref.title.op("!~")("[가-힣]")),
    )


def _domestic_first():
    return case((PaperCitationExternalRef.external_id.like("ART%"), 0), else_=1)


async def get_external_refs(
    db: AsyncSession,
    source_cn: str,
    direction: str,
    *,
    limit: int,
    excluded_ids: list[str] | None = None,
) -> list[PaperCitationExternalRef]:
    """코퍼스 밖 인용/피인용 대상 조회. excluded_ids는 external_id 기준 dedup용."""
    query = (
        select(PaperCitationExternalRef)
        .where(
            PaperCitationExternalRef.source_cn == source_cn,
            PaperCitationExternalRef.direction == direction,
            _not_in_corpus(),
            _displayable(),
        )
        .order_by(
            _domestic_first(),
            PaperCitationExternalRef.pubyear.desc().nulls_last(),
            PaperCitationExternalRef.title.asc(),
        )
        .limit(limit)
    )
    if excluded_ids:
        query = query.where(PaperCitationExternalRef.external_id.notin_(excluded_ids))

    result = await db.execute(query)
    return list(result.scalars().all())


async def count_remaining_external_refs(
    db: AsyncSession,
    source_cn: str,
    direction: str,
    *,
    excluded_ids: list[str] | None = None,
) -> int:
    query = select(func.count()).select_from(PaperCitationExternalRef).where(
        PaperCitationExternalRef.source_cn == source_cn,
        PaperCitationExternalRef.direction == direction,
        _not_in_corpus(),
        _displayable(),
    )
    if excluded_ids:
        query = query.where(PaperCitationExternalRef.external_id.notin_(excluded_ids))

    result = await db.execute(query)
    return result.scalar_one()


async def sources_with_remaining_external_refs(
    db: AsyncSession,
    source_cns: list[str],
    direction: str,
    *,
    excluded_ids: list[str],
) -> set[str]:
    """source_cns 중 화면에 아직 안 놓인 참고문헌/피인용 논문(external_refs)이 남은 것.
    in-service 노드의 has_more는 Neo4j CITES만 봐서는 부족하다 — 해외 논문만 있는 논문도 펼칠 수 있다."""
    if not source_cns:
        return set()
    query = (
        select(PaperCitationExternalRef.source_cn)
        .where(
            PaperCitationExternalRef.source_cn.in_(source_cns),
            PaperCitationExternalRef.direction == direction,
            PaperCitationExternalRef.external_id.notin_(excluded_ids or [""]),
            _not_in_corpus(),
            _displayable(),
        )
        .distinct()
    )
    result = await db.execute(query)
    return set(result.scalars().all())
