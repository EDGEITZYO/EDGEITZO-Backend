from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import success_response
from app.models.user import User
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.recent_read import RecentReadItem, RecentReadsResponse
from app.services.recent_read_service import (
    get_recent_reads_service,
    record_recent_read_service,
)

router = APIRouter(prefix="/recent-reads", tags=["RecentReads"])


@router.post(
    "/{paper_id}",
    response_model=ApiResponse[RecentReadItem],
    responses={
        401: {"model": ApiErrorResponse},
        404: {"model": ApiErrorResponse},
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="Record a recent read",
    description="Records a paper as recently read as soon as the detail page is viewed.",
)
async def record_recent_read(
    paper_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await record_recent_read_service(db, current_user, paper_id)
    return success_response(data=result, message="recent read recorded")


@router.get(
    "",
    response_model=ApiResponse[RecentReadsResponse],
    responses={
        401: {"model": ApiErrorResponse},
        422: {"model": ApiErrorResponse},
        500: {"model": ApiErrorResponse},
    },
    summary="List recent reads",
    description="Returns the current user's recently viewed papers.",
)
async def list_recent_reads(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await get_recent_reads_service(
        db,
        current_user,
        limit=limit,
        offset=offset,
    )
    return success_response(
        data=result,
        message="recent reads fetched",
        meta={
            "limit": limit,
            "offset": offset,
            "count": len(result.items),
        },
    )
