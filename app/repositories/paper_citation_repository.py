from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import PaperCitationExternalRef


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
    )
    if excluded_ids:
        query = query.where(PaperCitationExternalRef.external_id.notin_(excluded_ids))

    result = await db.execute(query)
    return result.scalar_one()
