from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.response import success_response
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.paper import PaperDetailResponse
from app.services.paper_service import get_paper_detail_service

router = APIRouter()


@router.get(
    "/papers/{paper_cn}",
    response_model=ApiResponse[PaperDetailResponse],
    responses={
        400: {"model": ApiErrorResponse},
        404: {"model": ApiErrorResponse},
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="논문 상세 조회",
    description="Neo4j Paper 상세 정보와 Journal ISSN 기반 신뢰도 배지를 조회합니다.",
)
async def get_paper_detail(
    paper_cn: str,
    db: AsyncSession = Depends(get_db),
):
    result = await get_paper_detail_service(paper_cn, db)

    return success_response(
        data=result,
        message="paper detail fetched",
        meta={
            "paper_cn": paper_cn,
            "keyword_count": len(result.keywords),
            "author_count": result.author_count,
        },
    )
