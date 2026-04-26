from fastapi import APIRouter, Query

from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.search import SearchPapersResponse
from app.services.semantic_scholar_service import semantic_scholar_search_service

router = APIRouter()


@router.get(
    "/semantic-scholar/search",
    response_model=ApiResponse[SearchPapersResponse],
    responses={
        500: {"model": ApiErrorResponse},
    },
    summary="Semantic Scholar 논문 검색 테스트",
)
async def semantic_scholar_search(
    query: str = Query(..., min_length=1),
    size: int = Query(10, ge=1, le=30),
):
    result = await semantic_scholar_search_service(query=query, size=size)

    return success_response(
        data=result,
        message="semantic scholar search completed",
        meta={
            "size": size,
            "count": len(result.items),
        },
    )