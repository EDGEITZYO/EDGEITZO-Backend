from fastapi import APIRouter

from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.search import SearchPapersRequest, SearchPapersResponse
from app.services.search_service import search_papers_service

router = APIRouter()


@router.post(
    "/search/papers",
    response_model=ApiResponse[SearchPapersResponse],
    responses={
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="논문 검색",
    description="자연어 검색어와 필터 조건을 받아 논문 검색 결과를 반환합니다.",
)
async def search_papers(request: SearchPapersRequest):
    result = await search_papers_service(request)

    return success_response(
        data=result,
        message="paper search completed",
        meta={
            "page": request.page,
            "size": request.size,
            "count": len(result.items),
        },
    )