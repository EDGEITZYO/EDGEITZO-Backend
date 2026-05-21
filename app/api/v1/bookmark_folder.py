from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import success_response
from app.models.user import User
from app.schemas.bookmark_folder import (
    BookmarkFolderCreate,
    BookmarkFolderResponse,
    BookmarkFolderUpdate,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.services.bookmark_folder_service import (
    create_folder,
    delete_folder,
    get_folders,
    update_folder,
)

router = APIRouter(prefix="/bookmark-folders", tags=["Bookmark"])


@router.get(
    "",
    response_model=ApiResponse[list[BookmarkFolderResponse]],
    responses={401: {"model": ApiErrorResponse}},
    summary="폴더 목록 조회",
    description="사용자의 북마크 폴더 목록을 반환합니다.",
)
async def list_folders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folders = await get_folders(db, current_user.id)
    return success_response(
        data=[BookmarkFolderResponse.model_validate(f) for f in folders],
        message="ok",
    )


@router.post(
    "",
    response_model=ApiResponse[BookmarkFolderResponse],
    responses={401: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    summary="폴더 생성",
)
async def create_bookmark_folder(
    body: BookmarkFolderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await create_folder(db, current_user.id, body.name)
    return success_response(
        data=BookmarkFolderResponse.model_validate(folder),
        message="폴더가 생성되었습니다",
    )


@router.patch(
    "/{folder_id}",
    response_model=ApiResponse[BookmarkFolderResponse],
    responses={401: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    summary="폴더명 수정",
)
async def update_bookmark_folder(
    folder_id: UUID,
    body: BookmarkFolderUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await update_folder(db, current_user.id, folder_id, body.name)
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="폴더를 찾을 수 없습니다")
    return success_response(
        data=BookmarkFolderResponse.model_validate(folder),
        message="폴더명이 수정되었습니다",
    )


@router.delete(
    "/{folder_id}",
    response_model=ApiResponse[None],
    responses={401: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    summary="폴더 삭제",
)
async def delete_bookmark_folder(
    folder_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_folder(db, current_user.id, folder_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="폴더를 찾을 수 없습니다")
    return success_response(message="폴더가 삭제되었습니다")
