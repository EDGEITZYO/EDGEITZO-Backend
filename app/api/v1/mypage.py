from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import success_response
from app.models.user import User
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.mypage import MypageProfile, MypageProfileUpdate, MypageResponse
from app.services.mypage_service import get_mypage_data, update_mypage_profile

router = APIRouter(prefix="/mypage", tags=["Mypage"])


@router.get(
    "",
    response_model=ApiResponse[MypageResponse],
    responses={401: {"model": ApiErrorResponse}},
    summary="마이페이지 조회",
    description="로그인한 사용자의 계정/프로필 정보와 마이페이지 활동 요약을 반환합니다.",
)
async def get_mypage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await get_mypage_data(db, current_user)
    return success_response(data=data, message="mypage fetched")


@router.patch(
    "/profile",
    response_model=ApiResponse[MypageProfile],
    responses={401: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    summary="마이페이지 프로필 수정",
    description="로그인한 사용자의 프로필 정보를 부분 수정합니다.",
)
async def update_profile(
    body: MypageProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await update_mypage_profile(
        db,
        current_user,
        body.model_dump(exclude_unset=True),
    )
    return success_response(data=data, message="profile updated")
