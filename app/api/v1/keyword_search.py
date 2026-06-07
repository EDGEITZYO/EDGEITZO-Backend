from __future__ import annotations

import json
import uuid as _uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.response import success_response
from app.models.user_keyword_map import UserKeywordMap
from app.schemas.search import PaperSearchItem
from app.services.chroma_search_service import get_chroma_search_service
from app.services.neo4j_search_service import get_paper_ids_by_keyword
from app.api.v1.home import save_search_history

router = APIRouter()

_REDIS_DB = 7
_MAP_TTL = 86400  # 24시간 — keyword_map.py와 동일값 (추후 공통 상수로 추출 가능)


class KeywordMapResponse(BaseModel):
    research_field: str = Field(description="사용자의 연구 분야", example="딥러닝")
    tree: dict = Field(description="키워드 트리 구조 (중첩 JSON)")


class KeywordPaperRequest(BaseModel):
    keyword: str = Field(description="검색할 키워드 (한국어)", example="딥러닝")
    keyword_en: str = Field("", description="검색할 키워드 (영어). Neo4j 실패 시 ChromaDB fallback 검색에 사용", example="deep learning")
    sort: Literal["citation", "date"] = Field("date", description="정렬 기준. 'citation': 인용수 내림차순, 'date': 발행일 내림차순")
    year_range: Optional[str] = Field(None, description="발행 연도 필터. '3y'(2023~), '5y'(2021~), '10y'(2016~), null=전체")
    page: int = Field(1, description="페이지 번호 (1부터 시작)")
    size: int = Field(30, description="페이지당 결과 수")
    user_id: Optional[str] = Field(None, description="검색 이력 저장용 유저 ID (선택). 제공 시 첫 페이지 반환 시 이력 저장")


class PaperResult(BaseModel):
    paper_id: str = Field(description="논문 고유 ID", example="JAKO202312345678")
    title: str = Field(description="논문 제목")
    authors: List[str] = Field(description="저자 목록", example=["홍길동", "김철수"])
    pub_year: Optional[int] = Field(None, description="발행 연도", example=2023)
    journal: Optional[str] = Field(None, description="학술지명", example="한국정보과학회논문지")
    paper_type: Optional[str] = Field(None, description="논문 유형. '저널' | '학위논문' | '학회' | null", example="저널")
    abstract: Optional[str] = Field(None, description="초록")
    keywords: List[str] = Field(description="논문 키워드")
    doi_url: Optional[str] = Field(None, description="DOI URL", example="https://doi.org/10.1234/example")
    scope_badge: Optional[str] = Field(None, description="논문 범위 뱃지. 'KCI' | null", example="KCI")
    citation_count: Optional[int] = Field(None, description="인용 수", example=42)
    relevance_score: float = Field(description="ChromaDB 유사도 점수 (0~1)")
    trust_badge: None = Field(None, description="신뢰도 뱃지 (MVP 이후 제공 예정)")
    keyword_map_data: None = Field(None, description="키워드맵 연결 데이터 (MVP 이후 제공 예정)")
    related_papers: List = Field(default=[], description="연관 논문 목록 (MVP 이후 제공 예정)")


class KeywordPaperResponse(BaseModel):
    keyword: str = Field(description="검색한 키워드")
    papers: List[PaperResult] = Field(description="논문 결과 목록")
    total: int = Field(description="필터/정렬 적용 후 전체 결과 수 (페이지네이션 기준)")


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


_PAPER_TYPE_MAP = {"JAKO": "저널", "JAFO": "저널", "DIKO": "학위논문", "CFKO": "학회"}


def _to_result(item: PaperSearchItem) -> PaperResult:
    db    = item.db_code or ""
    scope = "KCI" if db == "JAKO" else None
    trust = item.credibility.badge if item.credibility.badge != "unknown" else None
    return PaperResult(
        paper_id=item.paper_id,
        title=item.title,
        authors=[a.name for a in item.authors],
        pub_year=item.year,
        journal=item.journal_name,
        paper_type=_PAPER_TYPE_MAP.get(db),
        abstract=item.abstract,
        keywords=item.keywords,
        doi_url=item.doi or None,
        scope_badge=scope,
        citation_count=item.credibility.citation_count,
        relevance_score=item.score,
        trust_badge=None,
        keyword_map_data=None,
        related_papers=[],
    )


@router.get(
    "/keyword-search/map/{user_id}",
    summary="사용자 키워드맵 조회",
    description="""사용자의 연구 분야 키워드 트리를 반환합니다.

- Redis 캐시 우선 조회 (TTL 24시간), 미스 시 PostgreSQL fallback
- `user_id`가 유효한 UUID가 아닌 경우 DB 조회 없이 404 반환
- 키워드맵이 없으면 404 반환 → 먼저 `/api/v1/keyword-map/generate` 호출 필요
""",
)
async def get_keyword_map(user_id: str, db: AsyncSession = Depends(get_db)):
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

    # Redis 미스 → PostgreSQL fallback (UUID인 경우만)
    try:
        uid = _uuid.UUID(user_id)
    except ValueError:
        uid = None

    if uid is not None:
        row = (await db.execute(
            select(UserKeywordMap).where(UserKeywordMap.user_id == uid)
        )).scalar_one_or_none()

        if row is not None:
            # Redis 재캐시 후 반환
            r.set(
                f"keyword_map:{user_id}",
                json.dumps({"research_field": row.research_field, "tree": row.tree}, ensure_ascii=False),
                ex=_MAP_TTL,
            )
            return success_response(
                data=KeywordMapResponse(
                    research_field=row.research_field,
                    tree=row.tree,
                ),
                message="keyword map found",
            )

    raise HTTPException(status_code=404, detail="키워드맵이 없습니다. 먼저 키워드맵을 생성해주세요.")


@router.post(
    "/keyword-search/papers",
    summary="키워드 기반 논문 검색",
    description="""키워드 노드 클릭 시 호출. Neo4j → ChromaDB 순서로 논문을 조회합니다.

**동작 흐름**
1. Neo4j에서 키워드에 연결된 논문 ID 목록 조회
2. ChromaDB에서 해당 ID의 메타데이터 fetch (유사도 계산 없음)
3. Neo4j 실패(다운) 시 ChromaDB 하이브리드 검색으로 자동 fallback

**필터/정렬**
- `year_range`: `'3y'`(2023~) / `'5y'`(2021~) / `'10y'`(2016~) / null(전체)
- `sort`: `'date'`(발행일 내림차순, 기본값) / `'citation'`(인용수 내림차순)

**검색 이력**
- `user_id` 제공 + `page=1`일 때 Redis에 검색 이력 자동 저장 (실패해도 결과에 영향 없음)
""",
)
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

    # 6. 검색 이력 저장 (user_id 제공 시, 첫 결과 반환 시점)
    if request.user_id and request.page == 1:
        try:
            search_id = str(_uuid.uuid4())
            save_search_history(
                user_id=request.user_id,
                search_type="keyword",
                title=request.keyword,
                search_id=search_id,
                keyword_path=[request.keyword],  # breadcrumb로 현재 노드만
            )
        except Exception:
            pass  # 이력 저장 실패는 검색 결과에 영향 없음

    return success_response(
        data=KeywordPaperResponse(
            keyword=request.keyword,
            papers=[_to_result(p) for p in paged],
            total=len(items),
        ),
        message="keyword papers found",
    )
