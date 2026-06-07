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
    update_folder,
)
from app.services.bookmark_service import get_folders_enriched

router = APIRouter(prefix="/bookmark-folders", tags=["Bookmark"])


@router.get(
    "",
    response_model=ApiResponse[list[BookmarkFolderResponse]],
    responses={401: {"model": ApiErrorResponse}},
    summary="폴더 목록 조회",
    description=(
        "사용자의 북마크 폴더 목록을 반환합니다.\n\n"
        "- `paper_count`: 폴더 내 북마크 수\n"
        "- `representative_keywords`: 폴더 내 논문 키워드 상위 2개\n"
        "- `updated_at`: 마지막 북마크 추가 시각"
    ),
)
async def list_folders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folders = await get_folders_enriched(db, current_user.id)
    return success_response(data=folders, message="ok")


@router.post(
    "",
    response_model=ApiResponse[BookmarkFolderResponse],
    responses={401: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    summary="폴더 생성",
    description="북마크 폴더를 생성합니다. **Authorization 헤더에 Bearer 토큰 필요.** 폴더명은 최대 100자.",
)
async def create_bookmark_folder(
    body: BookmarkFolderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await create_folder(db, current_user.id, body.name)
    return success_response(
        data=BookmarkFolderResponse(
            id=folder.id,
            name=folder.name,
            created_at=folder.created_at,
        ),
        message="폴더가 생성되었습니다",
    )


@router.patch(
    "/{folder_id}",
    response_model=ApiResponse[BookmarkFolderResponse],
    responses={401: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    summary="폴더명 수정",
    description="폴더명을 수정합니다. **Authorization 헤더에 Bearer 토큰 필요.** 본인 폴더가 아니거나 존재하지 않으면 404 반환.",
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
        data=BookmarkFolderResponse(
            id=folder.id,
            name=folder.name,
            created_at=folder.created_at,
        ),
        message="폴더명이 수정되었습니다",
    )


@router.delete(
    "/{folder_id}",
    response_model=ApiResponse[None],
    responses={401: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    summary="폴더 삭제",
    description="폴더를 삭제합니다. **Authorization 헤더에 Bearer 토큰 필요.** 폴더 내 북마크도 함께 삭제됩니다. 본인 폴더가 아니거나 존재하지 않으면 404 반환.",
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
