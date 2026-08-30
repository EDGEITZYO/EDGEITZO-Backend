from __future__ import annotations

import logging
import uuid
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.schemas.search import (
    PaperSearchItem,
    SearchPapersRequest,
    SearchPapersResponse,
)
from app.repositories.paper_repository import get_paper_cards_batch
from app.services.bookmark_service import get_bookmarked_paper_ids
from app.services.chroma_search_service import get_chroma_search_service
from app.services.credibility_service import enrich_items_with_credibility, paper_type_label, resolve_paper_type

logger = logging.getLogger(__name__)

# ── 아래 가중치 상수는 관련도순 정렬에서 제외하기로 하고 비활성화 (백업용으로 주석 보존) ──
# # 연도 점수 가중치
# _SCORE_RECENT_HIGH = 0.25      # 최신 선호 목적, 2024년 이상
# _SCORE_RECENT_MID = 0.15       # 최신 선호 목적, 2022~2023년
# _SCORE_RECENT_LOW = 0.05       # 최신 선호 목적, 2020~2021년
# _SCORE_YEAR_HIGH = 0.2         # 일반 목적, 2024년 이상
# _SCORE_YEAR_MID = 0.1          # 일반 목적, 2021~2023년
#
# # 인용수 점수 가중치
# _SCORE_CITATION_MAX = 0.2      # 인용수 보너스 상한
# _SCORE_CITATION_DIVISOR = 1000 # 인용수 보너스 분모
#
# # 신뢰도 배지 가중치
# _SCORE_BADGE_HIGH = 0.2
# _SCORE_BADGE_MEDIUM = 0.1
#
# # 키워드 수 가중치
# _SCORE_KEYWORD_PER = 0.03
# _SCORE_KEYWORD_MAX = 0.15


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
            model=settings.llm_model_fast,
            temperature=0.3,
        )
        hypothetical = resp.text.strip()
        return f"{query} {hypothetical}" if hypothetical else query
    except Exception as e:
        logger.warning("HyDE 쿼리 확장 실패, 원본 사용: %s", e)
        return query


# ── 관련도순 정렬 원칙 위반(최신/인용수/배지/키워드로 순위 왜곡)으로 비활성화. 백업용으로 주석 보존 ──
# def _apply_scoring(items: list[PaperSearchItem], research_purpose_class: str = "neutral") -> list[PaperSearchItem]:
#     """research_purpose_class: node_intent_extractor의 정규식 분류 결과. 'recency'|'citation'|'neutral'"""
#     purpose_prefers_recent = research_purpose_class == "recency"
#     purpose_prefers_citation = research_purpose_class == "citation"
#
#     for item in items:
#         base = item.score  # ChromaDB RRF 점수 기반
#
#         if item.year:
#             if purpose_prefers_recent:
#                 if item.year >= 2024:
#                     base += _SCORE_RECENT_HIGH
#                 elif item.year >= 2022:
#                     base += _SCORE_RECENT_MID
#                 elif item.year >= 2020:
#                     base += _SCORE_RECENT_LOW
#             else:
#                 if item.year >= 2024:
#                     base += _SCORE_YEAR_HIGH
#                 elif item.year >= 2021:
#                     base += _SCORE_YEAR_MID
#
#         if purpose_prefers_citation and item.credibility.citation_count:
#             citation_bonus = min(item.credibility.citation_count / _SCORE_CITATION_DIVISOR, _SCORE_CITATION_MAX)
#             base += citation_bonus
#
#         if item.credibility.badge == "high":
#             base += _SCORE_BADGE_HIGH
#         elif item.credibility.badge == "medium":
#             base += _SCORE_BADGE_MEDIUM
#
#         base += min(len(item.keywords) * _SCORE_KEYWORD_PER, _SCORE_KEYWORD_MAX)
#         item.score = round(base, 4)
#
#     return items


def _sort_items(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    return sorted(items, key=lambda x: x.score, reverse=True)


_SORT_LABELS = {
    "relevance": "관련도 기준",
    "year_desc": "최신순",
    "year_asc": "오래된순",
    "citation_desc": "인용수 높은순",
}


def _apply_sort_order(items: list[PaperSearchItem], sort_order: str) -> list[PaperSearchItem]:
    """sort_order 기준 재정렬. 'relevance'는 이미 _sort_items()로 정렬된 순서를 그대로 유지."""
    if sort_order == "year_asc":
        return sorted(items, key=lambda i: (i.year is None, i.year or 0))
    if sort_order == "year_desc":
        return sorted(items, key=lambda i: (i.year is None, -(i.year or 0)))
    if sort_order == "citation_desc":
        return sorted(items, key=lambda i: i.credibility.citation_count or 0, reverse=True)
    return items


# similarity_score/matched_snippet은 scripts/embed_abstract_sentences.py가 사전계산한
# 캐시(문장 임베딩)를 쓰므로 후보 수와 무관하게 빠름 — n_results를 생략해 where절 필터를
# 통과한 전체 후보를 반환한다. kci_only/sci_only(PostgreSQL 조인 필요) 등 여기서 못 거르는
# 필터는 호출부(search_papers_service)가 전체 후보에 대해 적용한 뒤 마지막에 size로 자른다.
async def _search_chroma_local(
    query: str,
    pub_year_start: int | None = None,
    scope: str | None = None,
    paper_type: str | None = None,
) -> list[PaperSearchItem]:
    try:
        service = get_chroma_search_service()
        return await service.search(
            query=query,
            pub_year_start=pub_year_start,
            scope=scope,
            paper_type=paper_type,
        )
    except Exception:
        return []


async def search_papers_service(
    request: SearchPapersRequest,
    db: AsyncSession | None = None,
    user_id: UUID | None = None,
) -> SearchPapersResponse:
    query_text = " ".join([request.query, *request.keywords]) if request.keywords else request.query

    items = await _search_chroma_local(
        query=query_text,
        pub_year_start=request.pub_year_start,
        paper_type=request.paper_type,
    )

    if db is not None:
        try:
            db_extra = await get_paper_cards_batch(db, [i.paper_id for i in items])
            for item in items:
                degree = (db_extra.get(item.paper_id) or {}).get("degree")
                item.paper_type = paper_type_label(resolve_paper_type(item.db_code, degree))
        except Exception:
            pass

        try:
            items = await enrich_items_with_credibility(items, db)
        except Exception:
            pass

        if user_id is not None:
            try:
                bookmarked_ids = await get_bookmarked_paper_ids(db, user_id, [i.paper_id for i in items])
                for item in items:
                    item.is_bookmarked = item.paper_id in bookmarked_ids
            except Exception:
                pass

    if request.kci_only:
        items = [i for i in items if i.credibility.kci_registered]
    if request.sci_only:
        items = [i for i in items if i.credibility.sci_indexed]

    # items = _apply_scoring(items)  # 관련도순 정렬 원칙에 따라 비활성화 — 순수 RRF 점수(item.score)로만 정렬
    items = _sort_items(items)
    items = _apply_sort_order(items, request.sort_order)

    if request.size is not None:
        items = items[: request.size]

    return SearchPapersResponse(
        search_id=str(uuid.uuid4()),
        items=items,
    )


async def execute_search(
    search_params: dict,
    db: AsyncSession | None = None,
    *,
    filter_paper_type: str | None = None,
    filter_year: int | None = None,
    filter_kci: bool | None = None,
    filter_sci: bool | None = None,
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
        pub_year_start=pub_year_start,
        scope=scope,
    )
    items.extend(chroma_items)

    if db is not None:
        try:
            items = await enrich_items_with_credibility(items, db)
        except Exception:
            pass

    # items = _apply_scoring(items, research_purpose_class=research_purpose)  # 관련도순 정렬 원칙에 따라 비활성화
    items = _sort_items(items)

    # DB에서 degree 조회 → paper_type 세분화
    db_extra: dict = {}
    if db is not None:
        try:
            db_extra = await get_paper_cards_batch(db, [i.paper_id for i in items])
        except Exception:
            pass

    def _resolve_type(item: PaperSearchItem) -> str | None:
        degree = (db_extra.get(item.paper_id) or {}).get("degree")
        return paper_type_label(resolve_paper_type(item.db_code, degree))

    if filter_paper_type and filter_paper_type != "전체":
        items = [i for i in items if _resolve_type(i) == filter_paper_type]

    if filter_year is not None:
        items = [i for i in items if i.year == filter_year]

    if filter_kci is not None:
        items = [i for i in items if i.credibility.kci_registered is not None and i.credibility.kci_registered == filter_kci]

    if filter_sci is not None:
        items = [i for i in items if i.credibility.sci_indexed is not None and i.credibility.sci_indexed == filter_sci]

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
            "paper_type":     _resolve_type(item),
            "abstract":       item.abstract,
            "keywords":       item.keywords,
            "doi":            item.doi,
            "scope_badge":    _resolve_scope_badge(item.credibility),
            "citation_count": item.credibility.citation_count,
            "relevance_score": item.score,
            "trust_badge":    item.credibility.badge if item.credibility.badge != "unknown" else None,
            "keyword_map_data": None,
        })

    return {
        "papers": papers,
        "total": len(papers),
        "search_id": str(uuid.uuid4()),
    }
