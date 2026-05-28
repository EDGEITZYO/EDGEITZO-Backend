from __future__ import annotations
from fastapi import APIRouter, Query

from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.graph import (
    KeywordGraphResponse,
    KeywordPapersResponse,
    PaperNeighborGraphResponse,
)
from app.services.graph_service import (
    expand_keyword_graph_service,
    get_keyword_graph_service,
    get_keyword_papers_service,
    get_paper_neighbor_graph_service,
)

router = APIRouter()


@router.get(
    "/graph/keywords",
    response_model=ApiResponse[KeywordGraphResponse],
    responses={
        404: {"model": ApiErrorResponse},
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="초기 키워드 그래프 조회",
    description="키워드 이름 또는 key를 기준으로 중심 키워드와 관련 키워드 그래프를 조회합니다.",
)
async def get_keyword_graph(
    keyword: str = Query(..., min_length=1, description="키워드 이름 또는 key"),
    lang: str | None = Query(None, pattern="^(ko|en)$", description="키워드 언어"),
    limit: int = Query(20, ge=1, le=50, description="관련 키워드 최대 개수"),
    min_paper_count: int = Query(1, ge=1, description="최소 동시 등장 논문 수"),
):
    result = get_keyword_graph_service(
        keyword=keyword,
        lang=lang,
        limit=limit,
        min_paper_count=min_paper_count,
    )

    return success_response(
        data=result,
        message="keyword graph fetched",
        meta={
            "keyword": keyword,
            "lang": lang,
            "node_count": len(result.nodes),
            "edge_count": len(result.edges),
        },
    )


@router.get(
    "/graph/keywords/{keyword_key:path}/expand",
    response_model=ApiResponse[KeywordGraphResponse],
    responses={
        404: {"model": ApiErrorResponse},
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="키워드 노드 확장",
    description="선택된 키워드 key를 기준으로 연결된 관련 키워드 노드를 확장 조회합니다.",
)
async def expand_keyword_graph(
    keyword_key: str,
    limit: int = Query(20, ge=1, le=50, description="확장할 관련 키워드 최대 개수"),
    min_paper_count: int = Query(1, ge=1, description="최소 동시 등장 논문 수"),
):
    result = expand_keyword_graph_service(
        keyword_key=keyword_key,
        limit=limit,
        min_paper_count=min_paper_count,
    )

    return success_response(
        data=result,
        message="keyword graph expanded",
        meta={
            "keyword_key": keyword_key,
            "node_count": len(result.nodes),
            "edge_count": len(result.edges),
        },
    )


@router.get(
    "/graph/keywords/{keyword_key:path}/papers",
    response_model=ApiResponse[KeywordPapersResponse],
    responses={
        404: {"model": ApiErrorResponse},
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="키워드 연결 논문 리스트 조회",
    description="선택된 키워드 key와 연결된 논문 리스트를 조회합니다.",
)
async def get_keyword_papers(
    keyword_key: str,
    limit: int = Query(20, ge=1, le=50, description="논문 최대 개수"),
    offset: int = Query(0, ge=0, description="페이지네이션 시작 위치"),
):
    result = get_keyword_papers_service(
        keyword_key=keyword_key,
        limit=limit,
        offset=offset,
    )

    return success_response(
        data=result,
        message="keyword papers fetched",
        meta={
            "keyword_key": keyword_key,
            "limit": limit,
            "offset": offset,
            "count": len(result.items),
            "total_count": result.total_count,
        },
    )


@router.get(
    "/graph/papers/{paper_cn}/neighbors",
    response_model=ApiResponse[PaperNeighborGraphResponse],
    responses={
        404: {"model": ApiErrorResponse},
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="논문 주변 그래프 조회",
    description="논문 CN을 기준으로 Paper, Keyword, Author, Journal, Year 주변 그래프를 조회합니다.",
)
async def get_paper_neighbor_graph(
    paper_cn: str,
    keyword_limit: int = Query(30, ge=1, le=100, description="연결 키워드 최대 개수"),
):
    result = get_paper_neighbor_graph_service(
        paper_cn=paper_cn,
        keyword_limit=keyword_limit,
    )

    return success_response(
        data=result,
        message="paper neighbor graph fetched",
        meta={
            "paper_cn": paper_cn,
            "node_count": len(result.nodes),
            "edge_count": len(result.edges),
        },
    )
