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

_PAPER_TYPE_MAP: dict[str, str] = {
    "JAKO": "저널",
    "JAFO": "저널",
    "DIKO": "학위논문",
    "CFKO": "학회",
    "CFFO": "학회",
}


def _db_code_to_paper_type(db_code: str | None) -> str | None:
    if db_code is None:
        return None
    result = _PAPER_TYPE_MAP.get(db_code)
    if result is None:
        logger.warning("unknown DBCode for paper_type mapping: %s", db_code)
    return result


def _resolve_scope_badge(credibility) -> str | None:
    if credibility.kci_registered is True:
        return "KCI"
    if credibility.sci_indexed is True:
        return "SCI"
    return None


# C-2: HyDE 쿼리 확장 — 사용자 쿼리를 가상 논문 초록으로 변환해 임베딩 공간 정렬
# 짧은 쿼리와 긴 논문 초록 간 길이/표현 불균형 해소
async def expand_query_for_embedding(query: str) -> str:
    try:
        from app.services.llm.client import chat
        resp = await chat(
            messages=[{
                "role": "user",
                "content": (
                    f"다음 검색어에 대해 가상의 한국어 학술 논문 초록을 작성하라.\n"
                    f"검색어: \"{query}\"\n\n"
                    f"규칙:\n"
                    f"- 3~4문장의 자연스러운 논문 초록 형식\n"
                    f"- 연구 배경, 방법, 결과 흐름 포함\n"
                    f"- 실제 학술 용어 사용 (한글, 필요시 영문 병기)\n"
                    f"- 가상의 수치나 구체적 결론은 만들지 말 것\n\n"
                    f"초록만 반환 (제목, 설명, 따옴표 없이):"
                ),
            }],
            model=settings.llm_default_model,
            temperature=0.3,
            max_tokens=300,
        )
        hypothetical = resp.text.strip()
        return f"{query} {hypothetical}" if hypothetical else query
    except Exception as e:
        logger.warning("HyDE 쿼리 확장 실패, 원본 사용: %s", e)
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


async def execute_search(
    search_params: dict,
    db: AsyncSession | None = None,
    *,
    filter_paper_type: str | None = None,
    sort_order: str = "relevance",
) -> dict:
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

    if filter_paper_type and filter_paper_type != "전체":
        items = [i for i in items if _db_code_to_paper_type(i.db_code) == filter_paper_type]

    if sort_order == "year_asc":
        items = sorted(items, key=lambda i: (i.year is None, i.year or 0))
    elif sort_order == "year_desc":
        items = sorted(items, key=lambda i: (i.year is None, -(i.year or 0)))

    papers = []
    for item in items[:size]:
        papers.append({
            "paper_id":       item.paper_id,
            "title":          item.title,
            "authors":        [a.name for a in item.authors],
            "pub_year":       item.year,
            "journal":        item.journal_name,
            "paper_type":     _db_code_to_paper_type(item.db_code),
            "abstract":       item.abstract,
            "keywords":       item.keywords,
            "doi":            item.doi,
            "scope_badge":    _resolve_scope_badge(item.credibility),
            "citation_count": item.credibility.citation_count,
            "relevance_score": item.score,
            "trust_badge":    item.credibility.badge if item.credibility.badge != "unknown" else None,
            "keyword_map_data": None,
        })

    import uuid
    return {
        "papers": papers,
        "total": len(papers),
        "search_id": str(uuid.uuid4()),
    }
