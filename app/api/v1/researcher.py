from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user, get_current_user_optional
from app.core.response import success_response
from app.models.user import User
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.researcher import (
    RecentResearcherSearchResponse,
    ResearcherGraphResponse,
    ResearcherSearchResponse,
    SaveRecentResearcherSearchRequest,
)
from app.schemas.researcher_detail import (
    CoauthorListResponse,
    ResearchFlowResponse,
    PaperSortKey,
    ResearcherPaperListResponse,
    ResearcherPaperYearListResponse,
    ResearcherProfileResponse,
)
from app.services import researcher_detail_service, researcher_flow_service
from app.services.researcher_search_service import (
    build_researcher_graph,
    get_recent_researcher_searches,
    save_recent_researcher_search,
    search_researchers,
)

router = APIRouter(prefix="/researchers", tags=["Researcher"])


@router.get(
    "/search",
    response_model=ApiResponse[ResearcherSearchResponse],
    responses={422: {"model": ApiErrorResponse}},
    summary="연구자 탐색 검색",
    description=(
        "하나의 검색어를 연구자명 또는 연구 분야로 자동 판별해 연구자 목록을 반환합니다. "
        "연구자명과 매칭되면 이름 검색 결과를, 아니면 분야 검색 결과를 반환합니다."
    ),
)
async def search_researcher_endpoint(
    query: str = Query(..., min_length=1, description="연구자명 또는 연구 분야"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    result = await search_researchers(db, query, page=page, size=size)
    if current_user:
        save_recent_researcher_search(str(current_user.id), result.query, result.search_type)
    return success_response(
        data=result,
        message="researcher search completed",
        meta={"issue": 78, "count": len(result.items)},
    )


@router.get(
    "/field-graph",
    response_model=ApiResponse[ResearcherGraphResponse],
    responses={422: {"model": ApiErrorResponse}},
    summary="연구 분야 기반 연구자 그래프",
    description=(
        "연구 분야 검색 결과를 그래프 렌더링용 nodes/edges로 반환합니다. "
        "좌표 배치는 프론트엔드에서 수행하고, 백엔드는 관계 가중치와 피인용 수를 제공합니다."
    ),
)
async def get_researcher_field_graph(
    query: str = Query(..., min_length=1, description="연구 분야"),
    limit: int = Query(40, ge=1, le=100),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    result = await search_researchers(db, query, page=1, size=limit)
    graph = build_researcher_graph(result.query, result.items)
    if current_user:
        save_recent_researcher_search(str(current_user.id), result.query, "field")
    return success_response(
        data=graph,
        message="researcher field graph loaded",
        meta={"issue": 78, "count": max(len(graph.nodes) - 1, 0)},
    )


@router.get(
    "/recent-searches",
    response_model=ApiResponse[RecentResearcherSearchResponse],
    responses={401: {"model": ApiErrorResponse}},
    summary="연구자 탐색 최근 검색어",
)
async def get_recent_researcher_searches_endpoint(
    current_user: User = Depends(get_current_user),
):
    result = get_recent_researcher_searches(str(current_user.id))
    return success_response(data=result, message="researcher recent searches loaded")


@router.post(
    "/recent-searches",
    response_model=ApiResponse[RecentResearcherSearchResponse],
    responses={401: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    summary="연구자 탐색 최근 검색어 저장",
)
async def save_recent_researcher_search_endpoint(
    request: SaveRecentResearcherSearchRequest,
    current_user: User = Depends(get_current_user),
):
    save_recent_researcher_search(str(current_user.id), request.query, request.search_type)
    result = get_recent_researcher_searches(str(current_user.id))
    return success_response(data=result, message="researcher recent search saved")


# ─────────────────────────────────────────────────────────────────────────────
# 연구자 상세페이지 (명세 08-01 ~ 08-06)
# ※ 아래 경로들은 반드시 /search·/field-graph·/recent-searches 뒤에 등록되어야 한다.
#    FastAPI는 선언 순서로 매칭하므로, 위로 올리면 /{researcher_id}가 그것들을 가로챈다.
# ─────────────────────────────────────────────────────────────────────────────

_RID = PathParam(
    ...,
    description="연구자 ID. 연구자 탐색 결과나 공저자 팝오버에서 받은 값 (예: kci:3a6d8496972cf306)",
)


async def _ensure_exists(db: AsyncSession, researcher_id: str) -> None:
    profile = await researcher_detail_service.get_profile(db, researcher_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="해당 연구자를 찾을 수 없습니다"
        )


@router.get(
    "/{researcher_id}",
    response_model=ApiResponse[ResearcherProfileResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="연구자 프로필 기본 정보 (08-01)",
    description=(
        "상세페이지 상단 핵심 정보.\n\n"
        "- 미확보 값은 **null**로 내려갑니다. 화면의 '데이터 없음'·공란('-')은 프런트가 그립니다\n"
        "- `department`(전공)는 적재 커버리지가 17%라 대부분 null입니다 — 라벨째 숨기세요\n"
        "- `email`은 출처 신뢰도가 확실한 건만 내려갑니다(추정 건은 null)\n"
        "- `total_citations`는 `citation_source`가 kci냐 openalex냐에 따라 집계 범위가 달라 "
        "두 연구자의 수치를 그대로 비교하면 안 됩니다\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_profile(
    researcher_id: str = _RID,
    db: AsyncSession = Depends(get_db),
):
    profile = await researcher_detail_service.get_profile(db, researcher_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="해당 연구자를 찾을 수 없습니다"
        )
    return success_response(data=profile, message="researcher profile loaded")


@router.get(
    "/{researcher_id}/papers",
    response_model=ApiResponse[ResearcherPaperListResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="연구자 논문 리스트 (08-02)",
    description=(
        "연구자의 논문을 카드 목록으로 반환합니다. 기본 정렬은 최신순입니다.\n\n"
        "- `sort=citations`는 **`citation_sort_available`이 true일 때만** 드롭다운에 노출하세요. "
        "false인데 요청하면 최신순으로 되돌려 응답합니다(명세 08-02)\n"
        "- `coauthor_id`를 주면 그 공저자와 함께 쓴 논문만 남습니다 — 팝오버의 '함께 쓴 N편 보기'\n"
        "- `is_internal`이 false면 우리 상세페이지가 없습니다. `external_url`(KCI 원문)로 보내고, "
        "북마크(`can_bookmark`)·읽음(`read_at`)도 불가능합니다\n"
        "- `citation_count`가 null이면 미집계라 인용수 배지를 숨기세요(0과 구분 불가)\n"
        "- `sci_indexed`가 null이면 학술지 매칭 실패라 SCI 배지를 숨기세요. false는 '비SCI 확정'입니다\n"
        "- 논문이 0편이면 `items: []`, `total: 0` — 화면은 '등재된 논문이 없습니다'\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_papers(
    researcher_id: str = _RID,
    sort: PaperSortKey = Query("recent", description="recent=최신순(기본) | citations=피인용순"),
    page: int = Query(1, ge=1),
    size: int = Query(6, ge=1, le=100, description="와이어프레임 기준 6개 노출 후 페이지네이션"),
    coauthor_id: Optional[str] = Query(None, description="이 공저자와 함께 쓴 논문만 필터링"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_exists(db, researcher_id)
    result = await researcher_detail_service.get_papers(
        db,
        researcher_id,
        sort=sort,
        page=page,
        size=size,
        coauthor_id=coauthor_id,
        user_id=current_user.id if current_user else None,
    )
    return success_response(data=result, message="researcher papers loaded")


@router.get(
    "/{researcher_id}/papers/by-year",
    response_model=ApiResponse[ResearcherPaperYearListResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="연구자 논문 리스트 — 연도별 이력 (08-03)",
    description=(
        "08-02와 **같은 데이터를 발행연도로 묶기만** 한 응답입니다. 두 화면이 어긋나지 않도록 "
        "같은 조회를 씁니다.\n\n"
        "- 연도 내림차순(최신 연도 상단), 같은 연도 안에서는 발행월 내림차순\n"
        "- 발행연도가 없는 논문은 `year: null` 그룹으로 맨 뒤에 모입니다\n"
        "- 논문 카드의 각 필드 의미는 08-02와 동일합니다\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_papers_by_year(
    researcher_id: str = _RID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100, description="연도 그룹은 카드 여러 장을 담으므로 기본값이 더 큽니다"),
    coauthor_id: Optional[str] = Query(None, description="이 공저자와 함께 쓴 논문만 필터링"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_exists(db, researcher_id)
    result = await researcher_detail_service.get_papers_by_year(
        db,
        researcher_id,
        page=page,
        size=size,
        coauthor_id=coauthor_id,
        user_id=current_user.id if current_user else None,
    )
    return success_response(data=result, message="researcher papers by year loaded")


@router.get(
    "/{researcher_id}/coauthors",
    response_model=ApiResponse[CoauthorListResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="함께 연구한 사람들 (08-04)",
    description=(
        "같은 논문에 이름이 함께 올라간 연구자를, 함께 쓴 논문 수 내림차순으로 반환합니다.\n\n"
        "- 호버 팝오버에 필요한 값(이름·소속·전공·키워드·함께 쓴 논문 수)을 **목록 응답에 이미 포함**했습니다. "
        "팝오버를 띄울 때 추가 요청이 필요 없습니다\n"
        "- 모든 항목이 `researcher_id`를 가지므로 '상세 정보' 버튼은 항상 동작합니다\n"
        "- '함께 쓴 N편 보기'는 `GET /researchers/{researcher_id}/papers?coauthor_id=<그 사람 id>`\n"
        "- `department`(전공)는 대부분 null입니다\n"
        "- 공저자가 없으면 `total: 0` — 화면은 '0명'\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_coauthors(
    researcher_id: str = _RID,
    limit: int = Query(20, ge=1, le=100, description="아바타로 노출할 최대 인원"),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_exists(db, researcher_id)
    result = await researcher_detail_service.get_coauthors(db, researcher_id, limit=limit)
    return success_response(data=result, message="researcher coauthors loaded")


@router.get(
    "/{researcher_id}/research-flow",
    response_model=ApiResponse[ResearchFlowResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="연구 흐름 시각화 + 요약 카드 (08-05, 08-06)",
    description=(
        "연구자의 논문을 발행연도 순으로 배치한 노드 그래프와, 주제 묶음별 요약 카드를 함께 반환합니다.\n\n"
        "**그래프 (08-05)**\n"
        "- `nodes[].pub_year`/`pub_month`로 X축(좌 과거 → 우 최신) 배치를 계산하세요. "
        "좌표·노드 크기·제목 축약은 프런트가 정합니다\n"
        "- `edges`는 항상 과거(`source`) → 최신(`target`) 방향입니다. `weight`(0~1)를 선 굵기에 쓰세요\n"
        "- `cluster_id`가 같은 노드가 한 주제 묶음입니다. `is_core`가 대표 논문(진한 초록)입니다\n"
        "- `shared_keywords`가 비어 있어도 정상입니다 — 표기가 달라도 의미가 같으면 잇기 때문입니다\n\n"
        "**요약 카드 (08-06)**\n"
        "- `clusters[]`가 카드 한 장씩입니다. 카드 클릭 시 `node_ids`에 해당하는 노드를 하이라이트하세요\n"
        "- `topic`은 실제 논문 키워드만 근거로 AI가 문장화한 주제명이고, 그 근거가 `topic_keywords`입니다\n"
        "- `has_followup`이 false면 후속 논문이 없는 묶음이라 화면에 '후속 연구 없음'으로 표시합니다\n"
        "- `summary_source`가 `rule`이면 LLM 예산 소진 등으로 규칙 기반 문장이 나간 것입니다\n\n"
        "논문이 0편이어도 200으로 응답합니다(명세: 논문 수와 무관하게 상시 노출). "
        "첫 호출은 임베딩·요약 생성으로 수 초 걸리고, 이후에는 캐시에서 즉시 응답합니다.\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_research_flow(
    researcher_id: str = _RID,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_exists(db, researcher_id)
    result = await researcher_flow_service.get_research_flow(db, researcher_id)
    return success_response(
        data=result,
        message="researcher research flow loaded",
        meta={"summary_source": result.summary_source},
    )
