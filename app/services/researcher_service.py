from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.researcher import Researcher, ResearcherPaper
from app.schemas.keyword_map import ResearcherOut
from app.services.neo4j_search_service import get_paper_ids_by_keyword


async def get_related_researchers(keyword_key: str, db: AsyncSession, *, limit: int = 10) -> list[ResearcherOut]:
    """키워드에 연결된 논문들의 저자를 researcher_papers 조인으로 조회.
    데이터 적재 전에는 researcher_papers가 비어있으므로 항상 빈 리스트를 정상 반환한다."""
    paper_ids = await get_paper_ids_by_keyword(keyword_key)
    if not paper_ids:
        return []

    rows = (
        await db.execute(
            select(Researcher)
            .join(ResearcherPaper, ResearcherPaper.researcher_cn == Researcher.cn)
            .where(ResearcherPaper.paper_id.in_(paper_ids))
            .distinct()
            .limit(limit)
        )
    ).scalars().all()

    return [
        ResearcherOut(
            cn=r.cn,
            name_ko=r.author_name_kor,
            name_en=r.author_name_eng,
            institution_ko=r.author_inst_kor,
            article_count=r.article_cnt,
        )
        for r in rows
    ]
