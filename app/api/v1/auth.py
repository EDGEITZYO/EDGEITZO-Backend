from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from redis import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.redis_client import get_redis_client
from app.core.response import success_response
from app.core.settings import settings
from app.models.user import User
from app.schemas.auth import (
    EmailCheckRequest,
    LoginRequest,
    ProfileCreateRequest,
    RegisterRequest,
    SendCodeRequest,
    TokenResponse,
    VerifyCodeRequest,
)
from app.services.auth_service import (
    create_profile_service,
    email_check_service,
    login_service,
    oauth_callback_service,
    register_service,
    send_code_service,
    verify_code_service,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/email/check",
    summary="이메일 유효성 검사",
    description=(
        "이메일 형식 확인 및 중복 가입 여부를 검사합니다.\n\n"
        "- 형식 오류 시 422 반환 (Pydantic 자동 처리)\n"
        "- 이미 가입된 이메일이면 400 반환"
    ),
    responses={
        200: {"description": "사용 가능한 이메일"},
        400: {"description": "이미 가입된 이메일입니다"},
        422: {"description": "이메일 형식이 아닙니다"},
    },
)
async def check_email(
    body: EmailCheckRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await email_check_service(db, body.email)
    return success_response(data=result, message="사용 가능한 이메일입니다")


@router.post(
    "/email/send-code",
    summary="이메일 인증번호 발송",
    description=(
        "6자리 인증번호를 생성하여 이메일로 발송합니다.\n\n"
        "- Redis에 15분 TTL로 저장\n"
        "- 기존 실패 횟수 초기화\n"
        "- 이미 발송된 코드가 있어도 재발송 가능"
    ),
    responses={
        200: {"description": "인증번호 발송 성공"},
        500: {"description": "이메일 발송 실패"},
    },
)
async def send_code(
    body: SendCodeRequest,
    redis: Redis = Depends(get_redis_client),
):
    result = await send_code_service(redis, body.email)
    return success_response(data=result, message="인증번호가 발송되었습니다")


@router.post(
    "/email/verify-code",
    summary="이메일 인증번호 검증",
    description=(
        "발송된 인증번호를 검증합니다.\n\n"
        "- 인증번호 불일치 시 400 반환\n"
        "- 5회 연속 실패 시 코드 삭제 후 재발송 유도\n"
        "- 성공 시 verify:{email} 플래그를 15분간 Redis에 저장"
    ),
    responses={
        200: {"description": "인증이 완료되었습니다"},
        400: {"description": "인증번호 불일치 / 만료 / 5회 초과"},
    },
)
async def verify_code(
    body: VerifyCodeRequest,
    redis: Redis = Depends(get_redis_client),
):
    result = await verify_code_service(redis, body.email, body.code)
    return success_response(data=result, message="인증이 완료되었습니다")


@router.post(
    "/register",
    summary="이메일 회원가입",
    description=(
        "이메일 인증을 완료한 사용자가 비밀번호를 설정하여 회원가입합니다.\n\n"
        "- Redis verify:{email} 키로 인증 완료 여부 확인\n"
        "- 비밀번호 조건: 대/소문자/특수문자 포함, 8~20자\n"
        "- 가입 완료 시 Access Token + Refresh Token 발급\n"
        "- 회원가입 후 verify 키 삭제"
    ),
    responses={
        200: {"description": "회원가입 성공 및 토큰 발급"},
        400: {"description": "이메일 인증 미완료 또는 이미 가입된 이메일"},
        422: {"description": "비밀번호 조건 미충족 또는 불일치"},
    },
)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
):
    result = await register_service(db, redis, body.email, body.password)
    return success_response(data=TokenResponse(**result), message="회원가입이 완료되었습니다")


@router.post(
    "/login",
    summary="이메일 로그인",
    description=(
        "이메일과 비밀번호로 로그인합니다.\n\n"
        "- 미가입 이메일: 400 (회원가입 유도 메시지)\n"
        "- 비밀번호 불일치: 400 (오류 메시지)\n"
        "- 성공 시 Access Token + Refresh Token 발급"
    ),
    responses={
        200: {"description": "로그인 성공 및 토큰 발급"},
        400: {"description": "미가입 이메일 또는 비밀번호 불일치"},
        422: {"description": "요청 형식 오류"},
    },
)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await login_service(db, body.email, body.password)
    return success_response(data=TokenResponse(**result), message="로그인 성공")


@router.get(
    "/kakao/callback",
    summary="카카오 OAuth2 콜백",
    description=(
        "카카오 OAuth2 인증 완료 후 카카오 서버가 리디렉션하는 콜백 엔드포인트입니다.\n\n"
        "**⚠️ Swagger UI에서 직접 실행 불가 — 카카오 OAuth 흐름을 통해서만 호출됩니다.**\n\n"
        "1. code를 카카오 토큰으로 교환\n"
        "2. 카카오 사용자 정보 조회\n"
        "3. 신규 유저 → `/profile` 리디렉션 (프로필 생성 페이지)\n"
        "4. 기존 유저 (프로필 설정 완료) → `/main` 리디렉션\n"
        "5. 리디렉션 URL에 access_token, refresh_token 쿼리 파라미터 포함"
    ),
    responses={
        302: {"description": "신규 유저 → /profile, 기존 유저 → /main 으로 리디렉션"},
        400: {"description": "카카오 OAuth 처리 오류"},
    },
)
async def kakao_callback(
    code: str = Query(..., description="카카오 인증 서버가 전달하는 authorization code"),
    db: AsyncSession = Depends(get_db),
):
    access_token, refresh_token, is_new_user = await oauth_callback_service(db, "kakao", code)
    path = "/profile" if is_new_user else "/main"
    return RedirectResponse(
        url=f"{settings.frontend_url}{path}?access_token={access_token}&refresh_token={refresh_token}"
    )


@router.get(
    "/google/callback",
    summary="구글 OAuth2 콜백",
    description=(
        "구글 OAuth2 인증 완료 후 구글 서버가 리디렉션하는 콜백 엔드포인트입니다.\n\n"
        "**⚠️ Swagger UI에서 직접 실행 불가 — 구글 OAuth 흐름을 통해서만 호출됩니다.**\n\n"
        "1. code를 구글 토큰으로 교환\n"
        "2. 구글 사용자 정보 조회\n"
        "3. 신규 유저 → `/profile` 리디렉션 (프로필 생성 페이지)\n"
        "4. 기존 유저 (프로필 설정 완료) → `/main` 리디렉션\n"
        "5. 리디렉션 URL에 access_token, refresh_token 쿼리 파라미터 포함"
    ),
    responses={
        302: {"description": "신규 유저 → /profile, 기존 유저 → /main 으로 리디렉션"},
        400: {"description": "구글 OAuth 처리 오류"},
    },
)
async def google_callback(
    code: str = Query(..., description="구글 인증 서버가 전달하는 authorization code"),
    db: AsyncSession = Depends(get_db),
):
    access_token, refresh_token, is_new_user = await oauth_callback_service(db, "google", code)
    path = "/profile" if is_new_user else "/main"
    return RedirectResponse(
        url=f"{settings.frontend_url}{path}?access_token={access_token}&refresh_token={refresh_token}"
    )


@router.post(
    "/profile",
    summary="프로필 생성",
    description=(
        "회원가입 또는 소셜 로그인 후 사용자 프로필을 저장합니다.\n\n"
        "**JWT Bearer 토큰 인증 필수**\n\n"
        "저장 항목: 이름 / 성별 / 나이(나이대 문자열) / 역할 / 논문 탐색 목적(JSON 배열) / 기타 목적\n\n"
        "저장 완료 시 is_profile_set=True로 업데이트되고 서비스 시작 응답을 반환합니다."
    ),
    responses={
        200: {"description": "프로필 저장 완료 및 서비스 시작"},
        401: {"description": "JWT 토큰 없음 또는 만료"},
        422: {"description": "요청 형식 오류"},
    },
)
async def create_profile(
    body: ProfileCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await create_profile_service(db, current_user, body.model_dump())
    return success_response(data=result, message="프로필이 저장되었습니다. 서비스를 시작합니다")
