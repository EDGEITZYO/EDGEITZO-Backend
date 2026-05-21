from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import success_response
from app.models.user import User
from app.schemas.bookmark import BookmarkCheckResponse, BookmarkCreate, BookmarkResponse
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.services.bookmark_service import add_bookmark, check_bookmark, remove_bookmark

router = APIRouter(prefix="/bookmarks", tags=["Bookmark"])


@router.post(
    "",
    response_model=ApiResponse[BookmarkResponse],
    responses={401: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    summary="북마크 추가",
    description="논문을 북마크에 추가합니다. 이미 북마크된 경우 200을 반환합니다 (idempotent).",
)
async def create_bookmark(
    body: BookmarkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bm = await add_bookmark(db, current_user.id, body.paper_id, body.folder_id)
    return success_response(
        data=BookmarkResponse.model_validate(bm),
        message="북마크에 추가되었습니다",
    )


@router.delete(
    "/{paper_id}",
    response_model=ApiResponse[None],
    responses={401: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    summary="북마크 삭제",
)
async def delete_bookmark(
    paper_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await remove_bookmark(db, current_user.id, paper_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="북마크가 없습니다")
    return success_response(message="북마크가 삭제되었습니다")


@router.get(
    "/check/{paper_id}",
    response_model=ApiResponse[BookmarkCheckResponse],
    responses={401: {"model": ApiErrorResponse}},
    summary="북마크 여부 확인",
    description="해당 논문의 북마크 여부를 반환합니다.",
)
async def check_bookmark_status(
    paper_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    bookmarked = await check_bookmark(db, current_user.id, paper_id)
    return success_response(
        data=BookmarkCheckResponse(paper_id=paper_id, bookmarked=bookmarked),
        message="ok",
    )
