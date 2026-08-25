from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
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
