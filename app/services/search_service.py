from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.schemas.search import (
    PaperSearchItem,
    SearchPapersRequest,
    SearchPapersResponse,
)
from app.services.chroma_search_service import get_chroma_search_service
from app.services.credibility_service import enrich_items_with_credibility

logger = logging.getLogger(__name__)


# C-2: LLM 쿼리 확장 — 관련 학술 용어를 추가해 임베딩 품질 향상
async def expand_query_for_embedding(query: str) -> str:
    try:
        from app.services.llm.client import chat
        resp = await chat(
            messages=[{
                "role": "user",
                "content": (
                    f"다음 학술 검색어를 관련 학술 용어로 확장하라.\n"
                    f"원본: \"{query}\"\n"
                    f"규칙: 관련 개념어, 영문 용어 추가. 원본 의미 유지. 15단어 이내.\n"
                    f"확장된 검색어만 반환 (설명 없이):"
                ),
            }],
            model=settings.llm_default_model,
            temperature=0.0,
            max_tokens=100,
        )
        expanded = resp.text.strip()
        return f"{query} {expanded}" if expanded else query
    except Exception as e:
        logger.warning("쿼리 확장 실패, 원본 사용: %s", e)
        return query


def _apply_scoring(items: list[PaperSearchItem], research_purpose: str = "") -> list[PaperSearchItem]:
    purpose_prefers_recent = research_purpose in ("랩미팅발표", "최신트렌드")
    purpose_prefers_citation = research_purpose in ("논문작성참고", "연구주제탐색")

    for item in items:
        base = item.score  # ChromaDB RRF 점수 기반

        if item.year:
            if purpose_prefers_recent:
                if item.year >= 2024:
                    base += 0.25
                elif item.year >= 2022:
                    base += 0.15
                elif item.year >= 2020:
                    base += 0.05
            else:
                if item.year >= 2024:
                    base += 0.2
                elif item.year >= 2021:
                    base += 0.1

        if purpose_prefers_citation and item.credibility.citation_count:
            citation_bonus = min(item.credibility.citation_count / 1000, 0.2)
            base += citation_bonus

        if item.credibility.badge == "high":
            base += 0.2
        elif item.credibility.badge == "medium":
            base += 0.1

        base += min(len(item.keywords) * 0.03, 0.15)
        item.score = round(base, 4)

    return items


def _sort_items(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    return sorted(items, key=lambda x: x.score, reverse=True)


async def _search_chroma_local(
    query: str,
    size: int,
    pub_year_start: int | None = None,
    scope: str | None = None,
) -> list[PaperSearchItem]:
    try:
        service = get_chroma_search_service()
        return await service.search(
            query=query,
            n_results=size,
            pub_year_start=pub_year_start,
            scope=scope,
        )
    except Exception:
        return []


async def search_papers_service(
    request: SearchPapersRequest,
    db: AsyncSession | None = None,
) -> SearchPapersResponse:
    items: list[PaperSearchItem] = []

    chroma_items = await _search_chroma_local(query=request.query, size=request.size)
    items.extend(chroma_items)

    if db is not None:
        try:
            items = await enrich_items_with_credibility(items, db)
        except Exception:
            pass

    items = _apply_scoring(items)
    items = _sort_items(items)

    return SearchPapersResponse(
        search_id="search_combined_001",
        items=items[: request.size],
    )


async def execute_search(search_params: dict, db: AsyncSession | None = None) -> dict:
    """SearchParams 기반 실행 검색 — 슬롯 대화 완료 후 호출"""
    keywords = search_params.get("keywords") or []
    scope = search_params.get("scope", "ANY")
    pub_year_start = search_params.get("pub_year_start")
    research_purpose = search_params.get("research_purpose", "")
    size = 20

    base_query = " ".join(keywords)
    # C-2: LLM 쿼리 확장
    expanded_query = await expand_query_for_embedding(base_query)

    items: list[PaperSearchItem] = []

    chroma_items = await _search_chroma_local(
        query=expanded_query,
        size=size,
        pub_year_start=pub_year_start,
        scope=scope,
    )
    items.extend(chroma_items)

    if db is not None:
        try:
            items = await enrich_items_with_credibility(items, db)
        except Exception:
            pass

    items = _apply_scoring(items, research_purpose=research_purpose)
    items = _sort_items(items)

    papers = []
    for item in items[:size]:
        papers.append({
            "paper_id": item.paper_id,
            "title": item.title,
            "authors": [a.name for a in item.authors],
            "pub_year": item.year,
            "journal": item.journal_name,
            "paper_type": None,
            "abstract": item.abstract,
            "keywords": item.keywords,
            "scope_badge": None,  # TODO: DBCode → scope 배지 연결
            "citation_count": item.credibility.citation_count,
            "relevance_score": item.score,
            "trust_badge": None,   # TODO: 신뢰도 배지 연결 후 구현
            "keyword_map_data": None,  # TODO: 키워드맵 연결 후 구현
        })

    import uuid
    return {
        "papers": papers,
        "total": len(papers),
        "search_id": str(uuid.uuid4()),
    }
