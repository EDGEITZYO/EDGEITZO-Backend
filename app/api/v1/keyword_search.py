from __future__ import annotations

import json
from typing import List, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.redis import get_redis
from app.core.response import success_response
from app.schemas.search import PaperSearchItem
from app.services.chroma_search_service import get_chroma_search_service
from app.services.neo4j_search_service import get_paper_ids_by_keyword

router = APIRouter()

_REDIS_DB = 7


class KeywordMapResponse(BaseModel):
    research_field: str
    tree: dict


class KeywordPaperRequest(BaseModel):
    keyword: str
    keyword_en: str = ""
    sort: Literal["citation", "date"] = "date"
    year_range: Optional[str] = None   # "3y" | "5y" | "10y" | "all"
    page: int = 1
    size: int = 30


class PaperResult(BaseModel):
    paper_id: str
    title: str
    authors: List[str]
    pub_year: Optional[int]
    journal: Optional[str]
    paper_type: Optional[str]
    abstract: Optional[str]
    keywords: List[str]
    scope_badge: Optional[str]
    citation_count: Optional[int]
    relevance_score: float
    trust_badge: None = None
    keyword_map_data: None = None


class KeywordPaperResponse(BaseModel):
    keyword: str
    papers: List[PaperResult]
    total: int


def _year_cutoff(year_range: Optional[str]) -> Optional[int]:
    return {"3y": 2023, "5y": 2021, "10y": 2016}.get(year_range or "", None)


def _sort_items(items: list[PaperSearchItem], sort: str) -> list[PaperSearchItem]:
    if sort == "citation":
        return sorted(items, key=lambda x: x.credibility.citation_count or 0, reverse=True)
    # default: date desc
    return sorted(items, key=lambda x: x.year or 0, reverse=True)


def _apply_year_filter(items: list[PaperSearchItem], year_range: Optional[str]) -> list[PaperSearchItem]:
    cutoff = _year_cutoff(year_range)
    if not cutoff:
        return items
    return [i for i in items if i.year and i.year >= cutoff]


def _to_result(item: PaperSearchItem) -> PaperResult:
    return PaperResult(
        paper_id=item.paper_id,
        title=item.title,
        authors=[a.name for a in item.authors],
        pub_year=item.year,
        journal=item.journal_name,
        paper_type=None,
        abstract=item.abstract,
        keywords=item.keywords,
        scope_badge=None,
        citation_count=item.credibility.citation_count,
        relevance_score=item.score,
        trust_badge=None,
        keyword_map_data=None,
    )


@router.get("/keyword-search/map/{user_id}")
async def get_keyword_map(user_id: str):
    """사용자의 현재 연구분야 키워드맵 조회"""
    r = get_redis(_REDIS_DB)
    cached = r.get(f"keyword_map:{user_id}")
    if cached:
        data = json.loads(cached)
        return success_response(
            data=KeywordMapResponse(
                research_field=data.get("research_field", ""),
                tree=data.get("tree", {}),
            ),
            message="keyword map found",
        )
    # TODO: Neo4j 저장 구조 연동 후 DB 조회 추가
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="키워드맵이 없습니다. 먼저 키워드맵을 생성해주세요.")


@router.post("/keyword-search/papers")
async def search_papers_by_keyword(request: KeywordPaperRequest):
    """키워드 노드 클릭 → Neo4j로 논문 ID 조회 → ChromaDB에서 메타데이터 fetch.
    유사도 계산 없음. Neo4j 다운 시 ChromaDB 직접 검색으로 fallback.
    """
    service = get_chroma_search_service()

    # 1. Neo4j에서 Paper CN 목록 조회
    paper_ids = await get_paper_ids_by_keyword(request.keyword)

    if paper_ids:
        # 2a. ChromaDB collection.get() — 스코어링 없이 메타데이터만 fetch
        items = await service.get_items_by_ids(paper_ids)
    else:
        # 2b. fallback: Neo4j 다운 시 ChromaDB 하이브리드 검색
        query = f"{request.keyword} {request.keyword_en}".strip()
        items = await service.search(query=query, n_results=request.size * 2)

    # 3. 연도 필터
    items = _apply_year_filter(items, request.year_range)

    # 4. 정렬 (인용수 or 발행일)
    items = _sort_items(items, request.sort)

    # 5. 페이지네이션
    offset = (request.page - 1) * request.size
    paged = items[offset: offset + request.size]

    return success_response(
        data=KeywordPaperResponse(
            keyword=request.keyword,
            papers=[_to_result(p) for p in paged],
            total=len(items),
        ),
        message="keyword papers found",
    )
