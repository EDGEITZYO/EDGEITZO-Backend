from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper, PaperCitationExternalRef


def _not_in_corpus():
    """external_id가 papers에 실제로 존재하면 '코퍼스 밖'이 아니므로 제외하는 조건.

    적재 시점에 코퍼스 내부 판별이 느슨해 코퍼스 안 논문이 이 테이블에도 들어간 행이 있다.
    그대로 두면 같은 논문이 in_service=true/false 두 노드로 중복되거나, 화면에 아직 안 놓인
    경우 상세페이지도 못 가는 반쪽 노드로 나간다. 행 자체는 (Neo4j CITES에 누락된 인용관계를
    담고 있을 수 있어) 지우지 않고 조회 시점에만 걸러낸다."""
    return ~(
        select(Paper.id)
        .where(
            or_(
                Paper.id == PaperCitationExternalRef.external_id,
                Paper.kci_art_id == PaperCitationExternalRef.external_id,
            )
        )
        .exists()
    )


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
        )
        .order_by(PaperCitationExternalRef.pubyear.desc().nulls_last(), PaperCitationExternalRef.title.asc())
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
    )
    if excluded_ids:
        query = query.where(PaperCitationExternalRef.external_id.notin_(excluded_ids))

    result = await db.execute(query)
    return result.scalar_one()
