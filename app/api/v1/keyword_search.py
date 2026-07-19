from __future__ import annotations

import uuid as _uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.response import success_response
from app.models.user_keyword_map import UserKeywordMap
from app.schemas.common import ApiResponse
from app.schemas.paper import PaperListResponse
from app.services.keyword_map_service import get_node_papers

router = APIRouter()


class LastAnchorResponse(BaseModel):
    """세션 재개용 '마지막 조회 앵커'. 그래프 자체는 GET /keyword-map?keyword=(last_anchor_name_ko)로 재조회."""
    last_anchor_key: str = Field(description="마지막으로 조회한 앵커 키워드의 Neo4j key")
    last_anchor_name_ko: Optional[str] = Field(None, description="한글 표시명. 없으면 null")
    last_anchor_name_en: Optional[str] = Field(None, description="영문 표시명. 없으면 null")


class KeywordPaperRequest(BaseModel):
    keyword: str = Field(description="검색할 키워드 (한국어)", example="딥러닝")
    keyword_en: str = Field("", description="검색할 키워드 (영어). Neo4j 실패 시 ChromaDB fallback 검색에 사용", example="deep learning")
    sort: Literal["citation", "date"] = Field("date", description="정렬 기준. 'citation': 인용수 내림차순, 'date': 발행일 내림차순")
    year_range: Optional[str] = Field(None, description="발행 연도 필터. '3y'(2023~) / '5y'(2021~) / '10y'(2016~) / null(전체)")
    paper_type: Optional[str] = Field(None, description="논문 유형 필터. '학술 저널' | '박사학위 논문' | '석사학위 논문' | '학위논문' | null(전체)")
    kci: Optional[bool] = Field(None, description="KCI 등재 필터. true(KCI만) / false(비KCI만) / null(전체)")
    sci: Optional[bool] = Field(None, description="SCI 계열 필터. true(SCI만) / null(전체)")
    page: int = Field(1, description="페이지 번호 (1부터 시작)")
    size: int = Field(30, description="페이지당 결과 수")
    user_id: Optional[str] = Field(None, description="검색 이력 저장용 유저 ID (선택). 제공 시 첫 페이지 반환 시 이력 저장")
    map_session_id: Optional[str] = Field(None, description="키워드맵 생성 시 발급된 세션 ID. 제공 시 동일 세션 이력에 경로 누적")
    research_field: Optional[str] = Field(None, description="최상위 연구 분야 (탐색 이력 제목용)")


@router.get(
    "/keyword-search/map/{user_id}",
    response_model=ApiResponse[LastAnchorResponse],
    summary="사용자 마지막 조회 앵커 조회 (세션 재개)",
    description="""사용자가 마지막으로 조회한 키워드맵 앵커를 반환합니다 (그래프 트리 자체는 저장하지 않음).

- `user_id`가 유효한 UUID가 아니거나 저장된 이력이 없으면 404 반환
- 반환된 `last_anchor_name_ko`(또는 `_en`)로 `GET /api/v1/keyword-map?keyword=...`를 호출해 그래프를 다시 계산하면 됨
""",
)
async def get_last_anchor(user_id: str, db: AsyncSession = Depends(get_db)):
    try:
        uid = _uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="유효하지 않은 user_id 입니다.")

    row = (await db.execute(
        select(UserKeywordMap).where(UserKeywordMap.user_id == uid)
    )).scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="마지막으로 조회한 키워드맵이 없습니다.")

    return success_response(
        data=LastAnchorResponse(
            last_anchor_key=row.last_anchor_key,
            last_anchor_name_ko=row.last_anchor_name_ko,
            last_anchor_name_en=row.last_anchor_name_en,
        ),
        message="last anchor found",
    )


@router.post(
    "/keyword-search/papers",
    response_model=ApiResponse[PaperListResponse],
    response_description="필터/정렬/페이지네이션 적용된 논문 카드 목록. trust_badge는 papers 테이블 존재 시 채워짐",
    responses={
        200: {"description": "정상 응답. papers 빈 배열도 200 반환"},
        422: {"description": "요청 바디 유효성 오류 (필수 필드 누락 등)"},
    },
    summary="키워드 기반 논문 검색",
    description="""키워드 노드 클릭 시 호출. `app.services.keyword_map_service.get_node_papers`를 통해
`GET /keyword-map/node/{node_key}/papers`와 동일한 로직을 공유합니다 (중복 구현 없음).

**필터 파라미터**
- `year_range`: `'3y'`(2023~) / `'5y'`(2021~) / `'10y'`(2016~) / null(전체)
- `paper_type`: `'학술 저널'` / `'박사학위 논문'` / `'석사학위 논문'` / `'학위논문'` / null(전체)
- `kci`: `true`(KCI만) / `false`(비KCI만) / null(전체)
- `sci`: `true`(SCI 계열만) / null(전체)
- `sort`: `'date'`(발행일, 기본값) / `'citation'`(인용수)

**검색 이력**
- `user_id` 제공 + `page=1`일 때 Redis에 검색 이력 자동 저장 (실패해도 결과에 영향 없음)
""",
)
async def search_papers_by_keyword(
    request: KeywordPaperRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await get_node_papers(
        keyword=request.keyword,
        keyword_en=request.keyword_en,
        year_range=request.year_range,
        paper_type=request.paper_type,
        kci=request.kci,
        sci=request.sci,
        sort=request.sort,
        page=request.page,
        size=request.size,
        user_id=request.user_id,
        map_session_id=request.map_session_id,
        research_field=request.research_field,
        db=db,
    )
    return success_response(data=result, message="keyword papers found")
