from __future__ import annotations

import uuid as _uuid
from typing import Literal, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.response import success_response
from app.models.user_keyword_map import UserKeywordMap
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.keyword_map import (
    KeywordDefinitionOut,
    KeywordMapExpandRequest,
    KeywordMapExpandResponse,
    KeywordMapGraphResponse,
    KeywordMapNode,
    KeywordMapNodeDetailResponse,
    KeywordMapRecenterRequest,
)
from app.schemas.paper import PaperListResponse
from app.services.keyword_definition_service import get_keyword_definition
from app.services.keyword_map_service import (
    expand_node,
    get_initial_anchor_map,
    get_keyword_names,
    get_node_papers,
    recenter_keyword_map,
)
from app.services.researcher_service import get_related_researchers

router = APIRouter()


async def _save_last_anchor(db: AsyncSession, user_id: str, anchor: KeywordMapNode) -> None:
    """세션 재개용 '마지막 조회 앵커' upsert. user_id가 유효한 UUID가 아니면 조용히 무시(그래프 응답에 영향 없음)."""
    try:
        uid = _uuid.UUID(user_id)
    except ValueError:
        return

    stmt = pg_insert(UserKeywordMap).values(
        id=_uuid.uuid4(),
        user_id=uid,
        last_anchor_key=anchor.key,
        last_anchor_name_ko=anchor.name_ko,
        last_anchor_name_en=anchor.name_en,
    ).on_conflict_do_update(
        constraint="uq_user_keyword_maps_user_id",
        set_={
            "last_anchor_key": anchor.key,
            "last_anchor_name_ko": anchor.name_ko,
            "last_anchor_name_en": anchor.name_en,
            "updated_at": sa.func.now(),
        },
    )
    try:
        await db.execute(stmt)
        await db.commit()
    except Exception:
        await db.rollback()


@router.get(
    "/keyword-map",
    response_model=ApiResponse[KeywordMapGraphResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="키워드맵 최초 앵커 로드",
    description="""검색어를 화면 중앙 앵커로 고정하고, 빈도/동시출현 데이터로 좌(상위)/우(하위) 그래프를 즉시 계산합니다 (LLM 미사용).

- 상위/하위 분류: 후보 빈도가 앵커보다 크면 상위, 작거나 같으면 하위 (동률은 하위)
- 정렬: 동시출현 수(유사도) 내림차순
- `has_more_parents`/`has_more_children`: 화살표 활성화 여부
- `user_id` 제공 시 '마지막 조회 앵커'를 세션 재개용으로 저장 (`GET /keyword-search/map/{user_id}`에서 조회 가능)
""",
)
async def get_keyword_map(
    keyword: str = Query(..., min_length=1, description="검색어 (앵커로 고정될 키워드)"),
    user_id: str = Query(default="", description="세션 재개용 저장할 user_id (선택)"),
    db: AsyncSession = Depends(get_db),
):
    result = await get_initial_anchor_map(keyword)

    if user_id:
        await _save_last_anchor(db, user_id, result.anchor)

    return success_response(data=result, message="keyword map loaded")


@router.post(
    "/keyword-map/node/{node_key:path}/recenter",
    response_model=ApiResponse[KeywordMapGraphResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="재중심화",
    description="""클릭한 노드를 새 앵커로 삼아 그래프를 다시 계산합니다.

- 이전 앵커의 좌/우 배치는 확정된 빈도 비교 규칙을 그대로 적용 (별도 규칙 없음)
- `existing_node_keys`로 전달된 이전 화면의 형제 노드는, 새 그래프와 실제로 연결되면 cross-link로 유지되고 아니면 응답에서 자연히 빠짐
""",
)
async def recenter_node(
    node_key: str,
    request: KeywordMapRecenterRequest,
    user_id: str = Query(default="", description="세션 재개용 저장할 user_id (선택)"),
    db: AsyncSession = Depends(get_db),
):
    result = await recenter_keyword_map(node_key, request.existing_node_keys)

    if user_id:
        await _save_last_anchor(db, user_id, result.anchor)

    return success_response(data=result, message="keyword map recentered")


@router.post(
    "/keyword-map/node/{node_key:path}/expand",
    response_model=ApiResponse[KeywordMapExpandResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="노드 제자리 확장",
    description="""앵커 이동 없이 특정 노드의 다음 단계 하위 키워드만 제자리에서 펼칩니다.

- `existing_node_keys`: 현재 화면에 표시 중인 전체 노드 key (전역 중복 제거용, 필수)
- `current_tier`: 확장 대상 노드의 현재 tier. 신규 노드는 이 값+1로 배정됨
""",
)
async def expand_node_in_place(node_key: str, request: KeywordMapExpandRequest):
    return success_response(
        data=await expand_node(
            node_key,
            current_tier=request.current_tier,
            existing_node_keys=request.existing_node_keys,
        ),
        message="node expanded",
    )


@router.get(
    "/keyword-map/node/{node_key:path}/papers",
    response_model=ApiResponse[PaperListResponse],
    summary="키워드 노드 논문 목록",
    description="""노드 key로 연결된 논문 목록을 반환합니다 (Neo4j → ChromaDB → PostgreSQL 보강, 기존 동작 유지).

**필터 파라미터**
- `year_range`: `'3y'`(2023~) / `'5y'`(2021~) / `'10y'`(2016~) / null(전체)
- `paper_type`: `'학술 저널'` / `'박사학위 논문'` / `'석사학위 논문'` / null(전체)
- `kci`: `true`(KCI만) / `false`(비KCI만) / null(전체)
- `sci`: `true`(SCI 계열만) / null(전체)
- `sort`: `'date'`(발행일, 기본값) / `'citation'`(인용수)

**탐색 이력**
- `user_id` 제공 + `page=1`일 때 탐색 이력 자동 저장
""",
)
async def get_keyword_map_node_papers(
    node_key: str,
    year_range: Optional[str] = Query(None, description="'3y'|'5y'|'10y'|null"),
    paper_type: Optional[str] = Query(None, description="'학술 저널'|'박사학위 논문'|'석사학위 논문'"),
    kci: Optional[bool] = Query(None, description="KCI 등재 여부"),
    sci: Optional[bool] = Query(None, description="SCI 등재 여부"),
    sort: Literal["citation", "date"] = Query("date", description="'date'(발행일) | 'citation'(인용수)"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    user_id: Optional[str] = Query(None, description="탐색 이력 저장용 유저 ID (선택)"),
    keyword_path: Optional[str] = Query(None, description="탐색 경로 (콤마 구분)"),
    map_session_id: Optional[str] = Query(None, description="키워드맵 탐색 세션 ID"),
    research_field: Optional[str] = Query(None, description="최상위 연구 분야 (탐색 이력 제목용)"),
    db: AsyncSession = Depends(get_db),
):
    result = await get_node_papers(
        keyword=node_key,
        year_range=year_range,
        paper_type=paper_type,
        kci=kci,
        sci=sci,
        sort=sort,
        page=page,
        size=size,
        user_id=user_id,
        keyword_path=keyword_path,
        map_session_id=map_session_id,
        research_field=research_field,
        db=db,
    )
    return success_response(data=result, message="node papers fetched")


@router.get(
    "/keyword-map/node/{node_key:path}/detail",
    response_model=ApiResponse[KeywordMapNodeDetailResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="키워드 노드 상세 정보 (정의 + 연구자)",
    description="""임의의 노드 클릭 시 우측 패널에 표시할 정의/연구자 정보.

- `definition`: ScienceON TREND API 정의 우선, 없으면 관련 논문 초록 기반 LLM 요약. 최초 조회 시 생성 후 영속 캐시되어 이후엔 재계산하지 않음.
- `researchers`: 연구자 데이터 적재 전에는 항상 빈 배열.
- 연관 논문 리스트(제목/저자/연도/인용수)는 `GET .../papers`, 탐색 경로(breadcrumb)는 클라이언트가 관리 — 이 엔드포인트는 정의/연구자만 담당.
""",
)
async def get_node_detail(node_key: str, db: AsyncSession = Depends(get_db)):
    names = await get_keyword_names(node_key)
    if names is None:
        raise HTTPException(status_code=404, detail=f"keyword not found: {node_key}")
    name_ko, name_en = names

    definition: Optional[KeywordDefinitionOut] = await get_keyword_definition(node_key, name_ko, name_en, db)
    researchers = await get_related_researchers(node_key, db)

    return success_response(
        data=KeywordMapNodeDetailResponse(definition=definition, researchers=researchers),
        message="node detail fetched",
    )
