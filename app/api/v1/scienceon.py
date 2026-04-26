from fastapi import APIRouter, Query

from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.search import SearchPapersResponse
from app.services.scienceon_service import (
    scienceon_raw_service,
    scienceon_search_service,
)

router = APIRouter()


@router.get(
    "/scienceon/search",
    response_model=ApiResponse[SearchPapersResponse],
    responses={
        500: {"model": ApiErrorResponse},
    },
    summary="ScienceON 논문 검색 테스트",
    description="ScienceON API를 직접 호출해 논문 검색 결과를 확인합니다.",
)
async def scienceon_search(
    query: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=30),
):
    result = await scienceon_search_service(query=query, page=page, size=size)

    return success_response(
        data=result,
        message="scienceon search completed",
        meta={
            "page": page,
            "size": size,
            "count": len(result.items),
        },
    )


@router.get(
    "/scienceon/raw",
    responses={
        500: {"model": ApiErrorResponse},
    },
    summary="ScienceON raw XML 확인",
    description="ScienceON 원본 XML과 파싱 결과를 확인합니다.",
)
async def scienceon_raw(
    query: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    size: int = Query(3, ge=1, le=5),
):
    result = await scienceon_raw_service(query=query, page=page, size=size)

    return success_response(
        data=result,
        message="scienceon raw fetched",
    )