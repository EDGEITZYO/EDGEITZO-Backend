from fastapi import APIRouter, Query

from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.graph import KeywordGraphResponse
from app.services.graph_service import (
    expand_keyword_graph_service,
    get_keyword_graph_service,
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
    "/graph/keywords/{keyword_key}/expand",
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
