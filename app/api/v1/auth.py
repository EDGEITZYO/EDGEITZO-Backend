from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from redis import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_member, get_current_user, require_demo_mode
from app.core.rate_limit import limit_guest_issue
from app.core.redis_client import get_redis_client
from app.core.response import success_response
from app.core.security import bearer_scheme, decode_token
from app.core.settings import settings
from app.models.user import User
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.auth import (
    EmailCheckRequest,
    LoginRequest,
    ProfileCreateRequest,
    RegisterRequest,
    SendCodeRequest,
    StartResponse,
    TokenResponse,
    VerifyCodeRequest,
)
from app.services.auth_service import (
    create_guest_service,
    create_profile_service,
    email_check_service,
    login_service,
    logout,
    oauth_callback_service,
    refresh_tokens,
    register_service,
    send_code_service,
    verify_code_service,
)

_ACCESS_MAX_AGE = 60 * settings.jwt_access_expire_minutes
_REFRESH_MAX_AGE = 60 * 60 * 24 * settings.jwt_refresh_expire_days
_GUEST_REFRESH_MAX_AGE = 60 * 60 * 24 * settings.guest_token_expire_days


_IS_LOCAL = settings.app_env == "local"
_SECURE_COOKIE = not _IS_LOCAL
_SAMESITE = "lax" if _IS_LOCAL else "none"


def _set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    refresh_max_age: int = _REFRESH_MAX_AGE,
) -> None:
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=_SECURE_COOKIE,
        samesite=_SAMESITE,
        max_age=_ACCESS_MAX_AGE,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=_SECURE_COOKIE,
        samesite=_SAMESITE,
        max_age=refresh_max_age,
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
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis_client),
):
    result = await register_service(db, redis, body.email, body.password)
    _set_auth_cookies(response, result["access_token"], result["refresh_token"])
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
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await login_service(db, body.email, body.password)
    _set_auth_cookies(response, result["access_token"], result["refresh_token"])
    return success_response(data=TokenResponse(**result), message="로그인 성공")


@router.post(
    "/guest",
    response_model=ApiResponse[TokenResponse],
    summary="게스트 자동 발급 (데모 기간 전용)",
    description=(
        "로그인 없이 서비스를 체험할 수 있는 익명 게스트 계정을 만들고 토큰을 발급합니다.\n\n"
        "**`DEMO_MODE=true`일 때만 동작하며, 꺼져 있으면 404를 반환합니다.** 요청 본문은 없습니다.\n\n"
        "- 응답 형식은 `POST /auth/login`과 같습니다 (`data`에 토큰 + HttpOnly 쿠키 2개)\n"
        "- 온보딩 완료 상태(`is_profile_set=true`)로 생성되며 기본 프로필이 채워집니다. "
        "닉네임은 `게스트{번호}`로 계정마다 다릅니다\n"
        "- Refresh 토큰 만료는 `GUEST_TOKEN_EXPIRE_DAYS`(기본 30일), Access 토큰 만료는 회원과 같습니다. "
        "토큰 payload에 `guest: true`가 들어갑니다\n"
        "- 북마크·최근 열람·탐색 이력 등은 회원과 똑같이 계정별로 분리 저장됩니다\n"
        "- 게스트는 프로필 설정·수정(`POST /auth/profile`, `PATCH /mypage/profile`)과 "
        "회원 탈퇴(`DELETE /mypage/account`)를 쓸 수 없습니다 (403)\n"
        "- 검색/LLM 호출 API는 게스트 계정당 `GUEST_LLM_RATE_LIMIT`회/`LLM_RATE_WINDOW_SECONDS`로 제한됩니다 (429)\n"
        "- IP당 발급 횟수는 `GUEST_ISSUE_LIMIT_PER_IP`회/`GUEST_ISSUE_WINDOW_SECONDS`로 제한되며, "
        "넘으면 429와 `Retry-After` 헤더(초)를 반환합니다"
    ),
    responses={
        200: {"description": "게스트 생성 및 토큰 발급"},
        404: {"model": ApiErrorResponse, "description": "DEMO_MODE가 꺼져 있음"},
        429: {"model": ApiErrorResponse, "description": "IP당 발급 횟수 초과 (Retry-After 헤더 포함)"},
    },
    dependencies=[Depends(require_demo_mode), Depends(limit_guest_issue)],
)
async def issue_guest(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    result = await create_guest_service(db)
    _set_auth_cookies(
        response,
        result["access_token"],
        result["refresh_token"],
        refresh_max_age=_GUEST_REFRESH_MAX_AGE,
    )
    return success_response(data=TokenResponse(**result), message="게스트 체험을 시작합니다")


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
        "5. access_token, refresh_token을 HttpOnly 쿠키로 설정 후 리디렉션"
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
    response = RedirectResponse(url=f"{settings.frontend_url}{path}")
    _set_auth_cookies(response, access_token, refresh_token)
    return response


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
        "5. access_token, refresh_token을 HttpOnly 쿠키로 설정 후 리디렉션"
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
    response = RedirectResponse(url=f"{settings.frontend_url}{path}")
    _set_auth_cookies(response, access_token, refresh_token)
    return response


@router.post(
    "/profile",
    summary="프로필 생성",
    description=(
        "회원가입 또는 소셜 로그인 후 사용자 프로필을 저장합니다.\n\n"
        "**JWT Bearer 토큰 인증 필수**\n\n"
        "저장 항목: 이름 / 성별 / 출생연도 / 역할 / 전공·연구 분야 / 논문 탐색 목적(JSON 배열) / 기타 목적\n\n"
        "저장 완료 시 is_profile_set=True로 업데이트되고 서비스 시작 응답을 반환합니다."
    ),
    responses={
        200: {"description": "프로필 저장 완료 및 서비스 시작"},
        401: {"description": "JWT 토큰 없음 또는 만료"},
        403: {"description": "게스트 계정은 사용 불가"},
        422: {"description": "요청 형식 오류"},
    },
)
async def create_profile(
    body: ProfileCreateRequest,
    current_user: User = Depends(get_current_member),
    db: AsyncSession = Depends(get_db),
):
    result = await create_profile_service(db, current_user, body.model_dump(exclude_none=True))
    return success_response(data=result, message="프로필이 저장되었습니다. 서비스를 시작합니다")


@router.post(
    "/refresh",
    summary="토큰 갱신 (Rotation)",
    description=(
        "Refresh 토큰 쿠키로 새 Access + Refresh 토큰을 발급합니다.\n\n"
        "- 이전 Refresh 토큰은 즉시 블랙리스트에 등록됩니다 (Rotation)\n"
        "- 탈취된 Refresh 토큰으로 재사용 시 401 반환"
    ),
    responses={
        200: {"description": "새 토큰 발급 성공"},
        401: {"description": "유효하지 않거나 이미 사용된 refresh 토큰"},
    },
)
async def refresh(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh 토큰이 없습니다")
    result = await refresh_tokens(refresh_token)
    _set_auth_cookies(
        response,
        result["access_token"],
        result["refresh_token"],
        refresh_max_age=_GUEST_REFRESH_MAX_AGE if result["guest"] else _REFRESH_MAX_AGE,
    )
    return success_response(message="토큰이 갱신되었습니다")


@router.post(
    "/logout",
    summary="로그아웃",
    description=(
        "Access 토큰과 선택적으로 Refresh 토큰을 블랙리스트에 등록합니다.\n\n"
        "**JWT Bearer 토큰 인증 필수**\n\n"
        "- Access 토큰: 남은 만료 시간만큼 블랙리스트 TTL 설정\n"
        "- Refresh 토큰: body에 포함 시 함께 무효화"
    ),
    responses={
        200: {"description": "로그아웃 성공"},
        401: {"description": "JWT 토큰 없음 또는 만료"},
    },
)
async def logout_endpoint(
    request: Request,
    response: Response,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    current_user: User = Depends(get_current_user),
):
    access_token = (credentials.credentials if credentials else None) or request.cookies.get("access_token")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증이 필요합니다")
    refresh_token = request.cookies.get("refresh_token")
    await logout(access_token, refresh_token)
    response.delete_cookie("access_token", httponly=True, secure=_SECURE_COOKIE, samesite=_SAMESITE)
    response.delete_cookie("refresh_token", httponly=True, secure=_SECURE_COOKIE, samesite=_SAMESITE)
    return success_response(message="로그아웃되었습니다")


@router.get(
    "/start",
    response_model=StartResponse,
    summary="시작하기 — 인증 상태 분기",
    description=(
        "Authorization 헤더의 Access 토큰 유효성을 확인하여 이동할 경로를 안내합니다.\n\n"
        "- 토큰 없음 또는 무효: `{authenticated: false, next_route: '/login'}`\n"
        "- 유효한 토큰: `{authenticated: true, next_route: '/main', user_id: '...'}`"
    ),
)
async def start(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    token = request.cookies.get("access_token") or (credentials.credentials if credentials else None)
    if not token:
        return StartResponse(authenticated=False, next_route="/login")

    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return StartResponse(authenticated=False, next_route="/login")

    return StartResponse(
        authenticated=True,
        next_route="/main",
        user_id=payload.get("sub"),
    )


@router.get(
    "/me",
    summary="현재 로그인 유저 이름 조회",
    description="소셜 로그인 가입 시 이름 표시를 위한 조회 엔드포인트 (토큰 인증 필수)",
    responses={
        200: {"description": "조회 성공"},
        401: {"description": "JWT 토큰 없음 또는 만료"},
    },
)
async def get_me(current_user: User = Depends(get_current_user)):
    return success_response(data={"name": current_user.name or ""})
