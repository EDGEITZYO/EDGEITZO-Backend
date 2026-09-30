from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import limit_llm_calls
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


async def _resolve(db: AsyncSession, researcher_id: str) -> str:
    """실제 연구자 ID. 합쳐져 없어진 옛 ID면 남은 ID로 이어주고, 없는 ID면 404.

    응답의 researcher_id는 항상 남은 ID라, 옛 ID로 들어온 쪽은 그 값으로 바꿔 쓰면 된다.
    """
    resolved = await researcher_detail_service.resolve_researcher_id(db, researcher_id)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="해당 연구자를 찾을 수 없습니다"
        )
    return resolved


@router.get(
    "/{researcher_id}",
    response_model=ApiResponse[ResearcherProfileResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="연구자 프로필 기본 정보 (08-01)",
    description=(
        "연구자 핵심 정보.\n\n"
        "- 미확보 값은 **null**로 내려갑니다 (숫자 필드에 '데이터 없음' 같은 문자열을 넣지 않습니다)\n"
        "- `department`(전공): 3,684명 중 2,046명(56%) 보유. KCI가 학과를 따로 주지 않아, "
        "본인 최근 논문의 소속 문자열(예: '충북대학교 환경공학과')에서 뽑아 채운 값이 포함됩니다\n"
        "- `email`: 출처 신뢰도가 `confirmed`/`domain_verified`인 423건만 내려갑니다(전체의 11.5%). "
        "추정(`inferred`) 54건은 동명이인일 때 다른 사람의 주소일 수 있어 null 처리합니다. "
        "ScienceON 연구자 색인이 불완전해(매칭률 46%) 적재를 늘려도 이 비율은 잘 오르지 않습니다\n"
        "- `total_citations`: 연구자 논문 목록의 KCI 피인용 합계입니다. `citation_source`는 `kci`(3,438명) "
        "또는 null(집계 없음, 246명)입니다\n\n"
        "합쳐져 없어진 옛 ID로 요청하면 남은 연구자로 응답하고, 응답의 `researcher_id`는 남은 ID입니다.\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_profile(
    researcher_id: str = _RID,
    db: AsyncSession = Depends(get_db),
):
    profile = await researcher_detail_service.get_profile(db, await _resolve(db, researcher_id))
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
        "- **논문 상세 이동은 `detail_id`로** 합니다. 우리 DB 논문 ID가 있으면 그 값, 없으면 KCI 논문 ID(ART…)이고, "
        "KCI ID면 상세·북마크 API가 첫 요청 때 KCI에서 받아 적재합니다. `can_open_detail`·`can_bookmark`는 "
        "detail_id가 있으면 true입니다\n"
        "- `is_internal`: papers에 행이 **이미** 있는지. 연구자 논문의 96.5%가 true(2026-09-30)\n"
        "- `abstract`: 내부 논문으로 연결된 건의 96.1%에 있습니다(전체의 92.8%). "
        "is_internal이 false면 papers에 행이 없어 초록도 없습니다\n"
        "- 항목 모양은 기존 논문 카드(`PaperCardResponse` — 키워드 검색·키워드맵 논문 목록)와 같은 필드명·의미이고 "
        "`trust_badge`도 같은 구조입니다. `paper_id`는 상세·북마크에 바로 넣을 수 있는 ID입니다\n"
        "- 필터 `year`·`paper_type`·`kci`·`sci`는 키워드맵 논문 목록과 같은 이름·의미입니다. `total`은 필터 적용 후 건수입니다\n"
        "- `citation_count`: 0이면 0, 값이 없을 때만 null(기존 논문 리스트와 같은 규칙)\n"
        "- `sci_indexed`: null은 학술지 매칭 실패(5.0%), false는 비SCI 확정. is_internal과 무관하게 채워집니다\n"
        "- 논문이 없으면 `items: []`, `total: 0`\n\n"
        "합쳐져 없어진 옛 ID로 요청하면 남은 연구자로 응답하고, 응답의 `researcher_id`는 남은 ID입니다.\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_papers(
    researcher_id: str = _RID,
    sort: PaperSortKey = Query("recent", description="recent=최신순(기본) | citations=피인용순"),
    page: int = Query(1, ge=1),
    size: int = Query(6, ge=1, le=100, description="한 번에 반환할 논문 수"),
    coauthor_id: Optional[str] = Query(None, description="이 연구자와 공동 작성한 논문만 필터링"),
    year: Optional[int] = Query(None, description="발행 연도. 그 해 논문만. null이면 전체"),
    paper_type: Optional[str] = Query(None, description="'학술 저널'|'박사학위 논문'|'석사학위 논문'. null·'전체'면 필터 없음"),
    kci: Optional[bool] = Query(None, description="true면 KCI 등재 논문만, false면 비등재만. null이면 전체"),
    sci: Optional[bool] = Query(None, description="true면 SCI 계열 논문만, false면 그 외만. null이면 전체"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    researcher_id = await _resolve(db, researcher_id)
    result = await researcher_detail_service.get_papers(
        db,
        researcher_id,
        sort=sort,
        page=page,
        size=size,
        coauthor_id=(
            await researcher_detail_service.resolve_researcher_id(db, coauthor_id) or coauthor_id
            if coauthor_id else None
        ),
        user_id=current_user.id if current_user else None,
        year=year,
        paper_type=paper_type,
        kci=kci,
        sci=sci,
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
        "합쳐져 없어진 옛 ID로 요청하면 남은 연구자로 응답하고, 응답의 `researcher_id`는 남은 ID입니다.\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_papers_by_year(
    researcher_id: str = _RID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100, description="한 번에 반환할 논문 수. 연도 그룹으로 묶이므로 기본값이 더 큽니다"),
    coauthor_id: Optional[str] = Query(None, description="이 연구자와 공동 작성한 논문만 필터링"),
    year: Optional[int] = Query(None, description="발행 연도. 그 해 논문만. null이면 전체"),
    paper_type: Optional[str] = Query(None, description="'학술 저널'|'박사학위 논문'|'석사학위 논문'. null·'전체'면 필터 없음"),
    kci: Optional[bool] = Query(None, description="true면 KCI 등재 논문만, false면 비등재만. null이면 전체"),
    sci: Optional[bool] = Query(None, description="true면 SCI 계열 논문만, false면 그 외만. null이면 전체"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    researcher_id = await _resolve(db, researcher_id)
    result = await researcher_detail_service.get_papers_by_year(
        db,
        researcher_id,
        page=page,
        size=size,
        coauthor_id=(
            await researcher_detail_service.resolve_researcher_id(db, coauthor_id) or coauthor_id
            if coauthor_id else None
        ),
        user_id=current_user.id if current_user else None,
        year=year,
        paper_type=paper_type,
        kci=kci,
        sci=sci,
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
        "문자열로 뽑으면 1인 평균 53명이 나오지만 그중 대부분은 이름만 있어 다시 조회할 수 없고 "
        "동명이인 문제도 생깁니다. 셀프조인 결과는 **1인 평균 8.4명(중앙값 6, 최대 77)**이며 "
        "전부 조회 가능합니다 (2026-09-23 실측)\n"
        "- 집계는 코퍼스 논문(`researcher_papers`)과 KCI 이력(`researcher_external_papers`)을 "
        "UNION해서 냅니다. 코퍼스 논문이 없는 연구자(공저자 확장으로 적재된 1,270명)도 "
        "외부 논문 쪽에서 관계가 잡혀 전원 공저자를 갖습니다\n"
        "- 명세 08-04가 요구하는 항목을 목록 응답에 모두 담아 항목별 추가 조회가 필요 없습니다\n"
        "- 함께 쓴 논문 목록은 `GET /researchers/{researcher_id}/papers?coauthor_id=<공저자 id>`\n"
        "- `department`(전공)는 연구자의 56%만 있습니다. `department_display`는 전공이 없으면 소속 기관을 담고, "
        "`department_source`가 어느 쪽인지 알려줍니다\n"
        "- 공저자가 없으면 `total: 0` (연구자의 94.3%는 1명 이상 보유)\n\n"
        "합쳐져 없어진 옛 ID로 요청하면 남은 연구자로 응답하고, 응답의 `researcher_id`는 남은 ID입니다.\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_coauthors(
    researcher_id: str = _RID,
    limit: int = Query(20, ge=1, le=100, description="반환할 최대 공저자 수"),
    db: AsyncSession = Depends(get_db),
):
    researcher_id = await _resolve(db, researcher_id)
    result = await researcher_detail_service.get_coauthors(db, researcher_id, limit=limit)
    return success_response(data=result, message="researcher coauthors loaded")


@router.get(
    "/{researcher_id}/research-flow",
    dependencies=[Depends(limit_llm_calls)],
    response_model=ApiResponse[ResearchFlowResponse],
    responses={404: {"model": ApiErrorResponse}},
    summary="연구 흐름 — 분야별 논문·요약 (08-05, 08-06)",
    description=(
        "연구자의 논문을 의미가 가까운 것끼리 **분야**로 묶어, 분야마다 논문 목록과 설명 한 줄을, "
        "전체에 대해 한 줄 요약을 반환합니다.\n\n"
        "**분야 (`clusters`)**\n"
        "- 논문의 영문 제목+키워드를 BGE-m3-ko로 임베딩하고(한글·영문 논문을 같은 언어로 비교하기 위해), 두 묶음의 논문 사이 평균 코사인 유사도가 "
        "0.40 이상이면 같은 분야로 합칩니다. 개수는 내용이 정하며 **상한이 없습니다**\n"
        "- 동떨어진 논문 1편은 억지로 합치지 않고 1편짜리 분야로 둡니다. 1편짜리 분야도 `topic`(분야 제목)은 "
        "다른 분야와 같은 방식으로 쓰고, `description`은 항상 null입니다\n"
        "- 순서는 **마지막 논문이 최근인 순**(`end_year` 내림차순, 같으면 마지막 논문 발행월, 그다음 논문 수)이고, "
        "`cluster_id`는 이 순서의 0부터 시작하는 번호입니다\n"
        "- `papers`는 그 분야의 논문 **전부**이며 발행연도 오름차순(연도 미상은 끝)입니다. "
        "`citation_count`는 0이면 0, 못 불러온 경우만 null, `published_at`은 출처가 준 정밀도까지만(YYYY-MM-DD / YYYY-MM / YYYY)\n"
        "- `paper_count`는 `papers`의 길이와 같습니다\n\n"
        "**문장 (08-06)**\n"
        "- 묶음·논문·키워드는 전부 계산이 정하고, `topic`(분야명)·`description`(분야 안의 흐름 한 문장)·"
        "`summary`(전체 한 줄, 100자 이하)만 LLM이 씁니다 (명세 08-06: 'AI는 문장화만 담당')\n"
        "- `topic_keywords`가 `topic`의 근거입니다. 없는 주제가 섞였는지 이 값으로 대조할 수 있습니다\n"
        "- `summary_source=rule`이면 `summary`가 규칙 기반 문장입니다. LLM 예산 소진·응답 거부·파싱 실패, "
        "또는 LLM 요약이 100자를 넘은 경우입니다. LLM 호출 자체가 실패했으면 `topic`도 키워드 나열이고 "
        "`description`은 null입니다\n\n"
        "**`flow_level`** — `none`(논문 0~1편) / `single`(한 분야) / `flow`(분야 2개 이상)\n\n"
        "논문이 0편이어도 200으로 응답합니다(명세: 논문 수와 무관하게 상시 제공). "
        "첫 호출은 임베딩·LLM 문장 생성으로 논문 수에 따라 약 4~20초 걸리고(분야가 많을수록 길다), "
        "결과를 저장하므로 이후에는 즉시 응답합니다. 논문 목록이 바뀌면 다시 생성합니다.\n\n"
        "합쳐져 없어진 옛 ID로 요청하면 남은 연구자로 응답하고, 응답의 `researcher_id`는 남은 ID입니다.\n\n"
        "**404** — 없는 researcher_id"
    ),
)
async def get_researcher_research_flow(
    researcher_id: str = _RID,
    db: AsyncSession = Depends(get_db),
):
    researcher_id = await _resolve(db, researcher_id)
    result = await researcher_flow_service.get_research_flow(db, researcher_id)
    return success_response(
        data=result,
        message="researcher research flow loaded",
        meta={"summary_source": result.summary_source},
    )
