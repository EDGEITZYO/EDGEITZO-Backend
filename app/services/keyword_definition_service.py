from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.integrations.scienceon.trend_client import ScienceOnTrendClient, parse_trend_search_xml
from app.models.keyword_definition import KeywordDefinition
from app.models.paper import Paper
from app.schemas.keyword_map import KeywordDefinitionOut
from app.services.llm.client import chat
from app.services.neo4j_search_service import get_paper_ids_by_keyword

logger = logging.getLogger(__name__)

_DEFINITION_SYSTEM_PROMPT = """너는 학술 키워드의 의미를 설명하는 도우미다.

반드시 지켜야 할 규칙:
1. 1~2문장으로 간결하게 쓴다.
2. 전달받은 논문 제목/초록에 나타난 내용만 근거로 사용하고, 새로운 사실을 지어내지 않는다.
3. 완전한 문어체("~이다") 대신 명사형으로 압축해 끝맺는 개조식으로 쓴다.
4. 문장 외의 다른 텍스트(따옴표, 설명, 마크다운 등)는 출력하지 않는다."""


async def _summarize_from_abstracts(keyword_text: str, papers: list[Paper]) -> Optional[str]:
    """근거(논문 제목·초록)는 코드에서 조회한 사실이며, LLM은 이를 요약하는 역할만 한다
    (search_graph.py의 _build_summary와 동일한 '근거 기반 요약' 패턴)."""
    fact_lines = [f"키워드: {keyword_text}"]
    for p in papers:
        title = p.title or p.title_en or p.id
        abstract = p.abstract or p.abstract_en
        if not abstract:
            continue
        fact_lines.append(f"- {title}: {abstract[:500]}")

    if len(fact_lines) <= 1:
        return None

    user_prompt = (
        "실제 데이터:\n" + "\n".join(fact_lines) +
        "\n\n위 논문들의 공통 주제를 근거로 이 키워드의 정의를 1~2문장으로 작성하라."
    )

    try:
        resp = await chat(
            messages=[{"role": "user", "content": f"[System]\n{_DEFINITION_SYSTEM_PROMPT}\n\n[User]\n{user_prompt}"}],
            model=settings.llm_default_model,
            temperature=0.3,
        )
        text = resp.text.strip()
        return text or None
    except Exception:
        logger.warning("키워드 정의 LLM 요약 실패: %s", keyword_text, exc_info=True)
        return None


async def get_keyword_definition(
    keyword_key: str,
    name_ko: str | None,
    name_en: str | None,
    db: AsyncSession,
) -> Optional[KeywordDefinitionOut]:
    """TREND API 우선 조회 → 매칭 없으면 관련 논문 초록 기반 LLM 요약으로 폴백. 최초 조회 후 영속 캐시."""
    cached = (
        await db.execute(select(KeywordDefinition).where(KeywordDefinition.keyword_key == keyword_key))
    ).scalar_one_or_none()
    if cached is not None:
        return KeywordDefinitionOut(
            keyword_key=cached.keyword_key,
            definition=cached.definition,
            source_url=cached.source_url,
            source=cached.source,
        )

    query_text = name_ko or name_en
    if not query_text:
        return None

    definition: Optional[str] = None
    source_url: Optional[str] = None
    source: Optional[str] = None
    trend_cn: Optional[str] = None

    try:
        xml = await ScienceOnTrendClient().search_trends(query_text, search_field="TI", size=1)
        items = parse_trend_search_xml(xml)
    except Exception:
        logger.warning("TREND API 조회 실패: %s", query_text, exc_info=True)
        items = []

    matched = next((item for item in items if item.definition), None)
    if matched is not None:
        definition = matched.definition
        source_url = matched.definition_source_url
        source = "trend"
        trend_cn = matched.cn

    if definition is None:
        paper_ids = await get_paper_ids_by_keyword(keyword_key)
        papers: list[Paper] = []
        if paper_ids:
            limit = settings.keyword_map_definition_llm_max_abstracts
            papers = (
                await db.execute(select(Paper).where(Paper.id.in_(paper_ids[:limit])))
            ).scalars().all()
        definition = await _summarize_from_abstracts(query_text, papers)
        if definition is not None:
            source = "llm"

    if definition is None:
        return None

    row = KeywordDefinition(
        keyword_key=keyword_key,
        definition=definition,
        source_url=source_url,
        source=source,
        trend_cn=trend_cn,
    )
    try:
        db.add(row)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("키워드 정의 캐시 저장 실패 (race 가능성 포함, 응답에는 영향 없음): %s", keyword_key, exc_info=True)

    return KeywordDefinitionOut(keyword_key=keyword_key, definition=definition, source_url=source_url, source=source)
