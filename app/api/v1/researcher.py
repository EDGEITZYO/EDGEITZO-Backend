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
    ResearcherSearchSort,
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
    sort: ResearcherSearchSort = Query(
        "relevance",
        description="정렬 기준: relevance=관련도순(기본) | paper_count=논문개수순",
    ),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    result = await search_researchers(db, query, page=page, size=size, sort=sort)
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
    description="연구자 ID (예: kci:3a6d8496972cf306). 연구자 탐색·공저자 응답이 내려주는 값",
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
        "연구자 핵심 정보.\n\n"
        "- 미확보 값은 **null**로 내려갑니다 (숫자 필드에 '데이터 없음' 같은 문자열을 넣지 않습니다)\n"
        "- `department`(전공): 2,993명 중 497명(17%)만 보유 — 대부분 null\n"
        "- `email`: 출처 신뢰도가 `confirmed`/`domain_verified`인 434건만 내려갑니다. "
        "추정(`inferred`) 60건은 동명이인일 때 다른 사람의 주소일 수 있어 null 처리합니다\n"
        "- `total_citations`: `citation_source`가 kci면 국내 등재지, openalex면 국제 범위라 "
        "집계 기준이 다릅니다(중앙값 35배 차이). 출처가 다른 두 연구자의 값은 같은 척도가 아닙니다\n\n"
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
        "연구자의 논문 목록. 코퍼스 논문(researcher_papers)과 KCI 이력(researcher_external_papers)을 "
        "합쳐 중복을 제거한 결과입니다. 기본 정렬은 최신순(발행연도 내림차순)입니다.\n\n"
        "- `sort=citations`: `citation_sort_available`이 false(인용수 보유 논문 3편 미만)면 "
        "요청해도 최신순으로 되돌려 응답합니다 (명세 08-02)\n"
        "- `coauthor_id`: 그 연구자와 공동 작성한 논문만 남깁니다\n"
        "- `is_internal`: papers에 행이 있는지. false면 논문 상세·북마크·읽음이 성립하지 않고 "
        "`external_url`(KCI 원문)만 있습니다. 현재 연구자 논문의 93.6%가 false이며, "
        "`scripts/promote_researcher_papers.py` 적재가 진행될수록 줄어듭니다\n"
        "- `citation_count`: null은 미집계(34%), 0은 집계 결과 0 — 다른 의미입니다\n"
        "- `sci_indexed`: null은 학술지 매칭 실패(9%), false는 비SCI 확정. is_internal과 무관하게 채워집니다\n"
        "- 논문이 없으면 `items: []`, `total: 0`\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_papers(
    researcher_id: str = _RID,
    sort: PaperSortKey = Query("recent", description="recent=최신순(기본) | citations=피인용순"),
    page: int = Query(1, ge=1),
    size: int = Query(6, ge=1, le=100, description="한 번에 반환할 논문 수"),
    coauthor_id: Optional[str] = Query(None, description="이 연구자와 공동 작성한 논문만 필터링"),
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
        "08-02와 **같은 조회 결과를 발행연도로 묶은** 응답입니다. 두 응답이 어긋나지 않도록 "
        "같은 함수를 씁니다.\n\n"
        "- 연도 내림차순, 같은 연도 안에서는 발행월 내림차순\n"
        "- KCI는 발행일을 주지 않는 건이 많아 같은 달 안의 순서는 확정되지 않습니다\n"
        "- 발행연도가 없는 논문은 `year: null` 그룹으로 모입니다\n"
        "- 항목 필드 의미는 08-02와 동일합니다\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_papers_by_year(
    researcher_id: str = _RID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100, description="한 번에 반환할 논문 수. 연도 그룹으로 묶이므로 기본값이 더 큽니다"),
    coauthor_id: Optional[str] = Query(None, description="이 연구자와 공동 작성한 논문만 필터링"),
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
    summary="함께 연구한 사람들 — 공저자 (08-04)",
    description=(
        "같은 논문에 이름이 함께 올라간 연구자를 함께 쓴 논문 수 내림차순으로 반환합니다.\n\n"
        "- 논문의 `authors` 문자열이 아니라 **researcher_id 셀프조인**으로 집계합니다. "
        "문자열로 뽑으면 1인 평균 33명이 나오지만 그중 77%는 이름만 있어 다시 조회할 수 없고 "
        "동명이인(110쌍) 문제도 생깁니다. 셀프조인 결과는 1인 평균 5.8명(최대 30명)이며 전부 조회 가능합니다\n"
        "- 명세 08-04가 요구하는 항목을 목록 응답에 모두 담아 항목별 추가 조회가 필요 없습니다\n"
        "- 함께 쓴 논문 목록은 `GET /researchers/{researcher_id}/papers?coauthor_id=<공저자 id>`\n"
        "- `department`(전공)는 커버리지 17%라 대부분 null입니다\n"
        "- 공저자가 없으면 `total: 0` (연구자의 93%는 1명 이상 보유)\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_coauthors(
    researcher_id: str = _RID,
    limit: int = Query(20, ge=1, le=100, description="반환할 최대 공저자 수"),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_exists(db, researcher_id)
    result = await researcher_detail_service.get_coauthors(db, researcher_id, limit=limit)
    return success_response(data=result, message="researcher coauthors loaded")


@router.get(
    "/{researcher_id}/research-flow",
    response_model=ApiResponse[ResearchFlowResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="연구 흐름 — 주제 묶음·연결·요약 (08-05, 08-06)",
    description=(
        "연구자 논문의 주제 묶음과 논문 간 연결, 묶음별 요약을 반환합니다.\n\n"
        "**계산 방식**\n"
        "- 논문의 제목+키워드를 BGE-m3-ko로 임베딩해 ward 연결로 묶습니다. "
        "명세 원안인 '키워드 글자 공유'는 한 연구자의 논문 쌍 중 80~99%가 공유 0이라(실측) "
        "연결이 거의 만들어지지 않습니다\n"
        "- `edges`는 각 논문에서 '같은 묶음의 앞선 논문 중 가장 가까운 한 편'으로 잇습니다. "
        "방향은 항상 과거(`source`) → 최신(`target`)이고 `weight`는 코사인 유사도입니다\n"
        "- `shared_keywords`는 실제 공유 키워드이며, 연결 근거가 의미 유사도라 비어 있을 수 있습니다\n\n"
        "**요약 (08-06)**\n"
        "- 묶음·시작/최근 논문·키워드는 전부 계산이 정하고, `topic`과 `summary`만 LLM이 문장화합니다 "
        "(명세 08-06: 'AI는 문장화만 담당')\n"
        "- `topic_keywords`가 `topic`의 근거입니다. 없는 주제가 섞였는지 이 값으로 대조할 수 있습니다\n"
        "- `summary_source=rule`이면 LLM 예산 소진·응답 거부·파싱 실패로 규칙 기반 문장이 나간 것입니다\n\n"
        "논문이 0편이어도 200으로 응답합니다(명세: 논문 수와 무관하게 상시 제공). "
        "첫 호출은 임베딩·요약 생성으로 수 초 걸리고 결과를 저장하므로 이후에는 즉시 응답합니다.\n\n"
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
