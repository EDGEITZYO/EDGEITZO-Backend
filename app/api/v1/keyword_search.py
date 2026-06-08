from __future__ import annotations

import json
import uuid as _uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.response import success_response
from app.models.user_keyword_map import UserKeywordMap
from app.schemas.common import ApiResponse
from app.schemas.paper import PaperListResponse
from app.services.chroma_search_service import get_chroma_search_service
from app.services.neo4j_search_service import get_paper_ids_by_keyword
from app.services.paper_filter_service import apply_filters, apply_sort, build_paper_cards, paginate
from app.api.v1.home import save_search_history

router = APIRouter()

_REDIS_DB = 7
_MAP_TTL = 86400  # 24시간 — keyword_map.py와 동일값


class KeywordMapResponse(BaseModel):
    research_field: str = Field(description="사용자의 연구 분야", example="딥러닝")
    tree: dict = Field(description="키워드 트리 구조 (중첩 JSON)")


class KeywordPaperRequest(BaseModel):
    keyword: str = Field(description="검색할 키워드 (한국어)", example="딥러닝")
    keyword_en: str = Field("", description="검색할 키워드 (영어). Neo4j 실패 시 ChromaDB fallback 검색에 사용", example="deep learning")
    sort: Literal["citation", "date"] = Field("date", description="정렬 기준. 'citation': 인용수 내림차순, 'date': 발행일 내림차순")
    year_range: Optional[str] = Field(None, description="발행 연도 필터. '3y'(2023~) / '5y'(2021~) / '10y'(2016~) / null(전체)")
    paper_type: Optional[str] = Field(None, description="논문 유형 필터. '저널' | '학위논문' | '학회' | null(전체)")
    kci: Optional[bool] = Field(None, description="KCI 등재 필터. true(KCI만) / false(비KCI만) / null(전체)")
    sci: Optional[bool] = Field(None, description="SCI 계열 필터. true(SCI만) / null(전체)")
    page: int = Field(1, description="페이지 번호 (1부터 시작)")
    size: int = Field(30, description="페이지당 결과 수")
    user_id: Optional[str] = Field(None, description="검색 이력 저장용 유저 ID (선택). 제공 시 첫 페이지 반환 시 이력 저장")


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
    response_model=ApiResponse[PaperListResponse],
    response_description="필터/정렬/페이지네이션 적용된 논문 카드 목록. trust_badge는 papers 테이블 존재 시 채워짐",
    responses={
        200: {"description": "정상 응답. papers 빈 배열도 200 반환"},
    },
    summary="키워드 기반 논문 검색",
    description="""키워드 노드 클릭 시 호출. Neo4j → ChromaDB 순서로 논문을 조회합니다.

**동작 흐름**
1. Neo4j에서 키워드에 연결된 논문 ID 목록 조회
2. ChromaDB에서 해당 ID의 메타데이터 fetch (유사도 계산 없음)
3. Neo4j 실패(다운) 시 ChromaDB 하이브리드 검색으로 자동 fallback
4. PostgreSQL IN 쿼리 1회로 `citation_count`, `kci_registered`, `sci_indexed`, `trust_badge` 보강

**필터 파라미터**
- `year_range`: `'3y'`(2023~) / `'5y'`(2021~) / `'10y'`(2016~) / null(전체)
- `paper_type`: `'저널'` / `'학위논문'` / `'학회'` / null(전체)
- `kci`: `true`(KCI만) / `false`(비KCI만) / null(전체)
- `sci`: `true`(SCI 계열만) / null(전체) — 현재 SCI 데이터 미수집으로 true 시 결과 없을 수 있음
- `sort`: `'date'`(발행일 내림차순, 기본값) / `'citation'`(인용수 내림차순)

**검색 이력**
- `user_id` 제공 + `page=1`일 때 Redis에 검색 이력 자동 저장 (실패해도 결과에 영향 없음)
""",
)
async def search_papers_by_keyword(
    request: KeywordPaperRequest,
    db: AsyncSession = Depends(get_db),
):
    service = get_chroma_search_service()

    paper_ids = await get_paper_ids_by_keyword(request.keyword)

    if paper_ids:
        items = await service.get_items_by_ids(paper_ids)
    else:
        query = f"{request.keyword} {request.keyword_en}".strip()
        items = await service.search(query=query, n_results=request.size * 2)

    items = apply_filters(
        items,
        year_range=request.year_range,
        paper_type=request.paper_type,
        kci=request.kci,
        sci=request.sci,
    )
    items = apply_sort(items, request.sort)
    paged, total = paginate(items, request.page, request.size)

    if request.user_id and request.page == 1:
        try:
            save_search_history(
                user_id=request.user_id,
                search_type="keyword",
                title=request.keyword,
                search_id=str(_uuid.uuid4()),
                keyword_path=[request.keyword],
            )
        except Exception:
            pass

    cards = await build_paper_cards(paged, db)
    return success_response(
        data=PaperListResponse(
            keyword=request.keyword,
            papers=cards,
            total=total,
            page=request.page,
            size=request.size,
        ),
        message="keyword papers found",
    )
