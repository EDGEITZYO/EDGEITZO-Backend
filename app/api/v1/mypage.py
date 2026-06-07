from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import success_response
from app.core.security import bearer_scheme
from app.models.user import User
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.mypage import MypageProfile, MypageProfileUpdate, MypageResponse
from app.services.auth_service import logout
from app.services.mypage_service import delete_account, get_mypage_data, update_mypage_profile

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


@router.delete(
    "/account",
    response_model=ApiResponse[None],
    responses={401: {"model": ApiErrorResponse}},
    summary="회원 탈퇴",
    description="""회원 탈퇴 처리 후 토큰을 무효화합니다. **Authorization 헤더에 Bearer 토큰 필요.**

삭제되는 데이터:
- 북마크 및 북마크 폴더
- 최근 열람 기록
- 키워드맵
- 유저 계정

토큰은 탈퇴 즉시 블랙리스트 처리되며 재사용 불가합니다.
""",
)
async def delete_account_endpoint(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await delete_account(db, current_user)
    await logout(credentials.credentials, None)
    return success_response(message="회원 탈퇴가 완료되었습니다")
