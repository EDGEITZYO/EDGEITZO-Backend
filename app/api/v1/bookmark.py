from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import success_response
from app.models.user import User
from app.schemas.bookmark import (
    BookmarkCheckResponse,
    BookmarkCreate,
    BookmarkListResponse,
    BookmarkResponse,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.services.bookmark_service import (
    add_bookmark,
    check_bookmark,
    get_bookmarks,
    remove_bookmark,
)

router = APIRouter(prefix="/bookmarks", tags=["Bookmark"])


@router.get(
    "",
    response_model=ApiResponse[BookmarkListResponse],
    responses={401: {"model": ApiErrorResponse}},
    summary="북마크 목록 조회",
    description=(
        "사용자의 북마크 목록을 반환합니다.\n\n"
        "**정렬 (`sort`)**\n"
        "- `bookmark_latest`: 최신 북마크 순 (기본)\n"
        "- `bookmark_oldest`: 오래된 북마크 순\n"
        "- `pubyear_latest`: 출판연도 최신 순\n"
        "- `pubyear_oldest`: 출판연도 오래된 순\n\n"
        "**논문 유형 필터 (`paper_type_filter`)**\n"
        "- `all`: 전체 (기본, excluded_non_stem 포함)\n"
        "- `journal`: 학술지 논문\n"
        "- `thesis`: 학위논문 (박사+석사 통합)\n"
        "- `conference`: 학술대회 (현재 데이터 없음)\n\n"
        "**검색 (`search_query`)**: 제목·저자·키워드 부분일치, 완전/접두/부분 순 우선\n\n"
        "**`paper.paper_type` 값 범위**\n"
        "- `'박사 학위 논문'`: DIKO + 박사 학위\n"
        "- `'석사 학위 논문'`: DIKO + 석사 학위\n"
        "- `'학술 저널'`: JAKO/JAFO/CFKO 코드 논문 (학술지·학술대회 통합)\n"
        "- `null`: 분류 불가\n\n"
        "**`paper.trust_badge` 구조 (PaperCardTrustBadge — 4필드)**\n"
        "- `kci` (bool | null): KCI 등재 여부\n"
        "- `sci` (bool | null): SCI 등재 여부\n"
        "- `citation_count` (int | null): 인용 수\n"
        "- `degree_type` (str | null): 학위 유형. `'박사 학위 논문'` | `'석사 학위 논문'` | null (학위논문에만 존재)\n\n"
        "**`degree_type` 값 범위**: DIKO 코드(학위논문) 논문만 채워짐. 학술 저널은 항상 null"
    ),
)
async def list_bookmarks(
    folder_id: Optional[UUID] = Query(default=None, description="폴더 필터 (미지정 시 전체)"),
    sort: Literal[
        "bookmark_latest", "bookmark_oldest", "pubyear_latest", "pubyear_oldest"
    ] = Query(default="bookmark_latest"),
    paper_type_filter: Literal["all", "journal", "thesis_phd", "thesis_master", "conference"] = Query(
        default="all"
    ),
    search_query: Optional[str] = Query(default=None, description="제목/저자/키워드 검색"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await get_bookmarks(
        db,
        current_user.id,
        folder_id=folder_id,
        page=page,
        size=size,
        sort=sort,
        paper_type_filter=paper_type_filter,
        search_query=search_query or None,
    )
    return success_response(data=data, message="ok")


@router.post(
    "",
    response_model=ApiResponse[BookmarkResponse],
    responses={401: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    summary="북마크 추가",
    description="논문을 북마크에 추가합니다. **Authorization 헤더에 Bearer 토큰 필요.** 이미 북마크된 경우 200을 반환합니다 (idempotent). `folder_id` 미지정 시 폴더 없이 저장됩니다.",
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
    description="논문 북마크를 삭제합니다. **Authorization 헤더에 Bearer 토큰 필요.** 북마크가 없으면 404 반환.",
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
    description="특정 논문의 북마크 여부를 확인합니다. **Authorization 헤더에 Bearer 토큰 필요.** 논문 상세 페이지 진입 시 북마크 버튼 상태 표시에 사용합니다.",
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
