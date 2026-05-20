from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException, status
from redis import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.email import generate_code, send_verification_email
from app.core.security import (
    _decode_without_blacklist_check,
    add_to_blacklist,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.settings import settings
from app.models.user import User
from app.repositories.user_repository import (
    create_user,
    get_user_by_email,
    get_user_by_provider,
    update_user,
)

_CODE_TTL = 900  # 15분
_MAX_FAIL = 5


async def email_check_service(db: AsyncSession, email: str) -> dict[str, Any]:
    user = await get_user_by_email(db, email)
    if user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미 가입된 이메일입니다",
        )
    return {"message": "사용 가능한 이메일입니다"}


async def send_code_service(redis: Redis, email: str) -> dict[str, Any]:
    code = generate_code()
    redis.setex(f"code:{email}", _CODE_TTL, code)
    redis.delete(f"code_fail:{email}")
    await send_verification_email(email, code)
    return {"message": "인증번호가 발송되었습니다"}


async def verify_code_service(redis: Redis, email: str, code: str) -> dict[str, Any]:
    stored = redis.get(f"code:{email}")
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증번호가 만료되었습니다. 재발송해주세요",
        )

    fail_key = f"code_fail:{email}"

    if stored != code:
        fail_count = int(redis.get(fail_key) or 0) + 1
        if fail_count >= _MAX_FAIL:
            redis.delete(f"code:{email}")
            redis.delete(fail_key)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="인증 시도 횟수를 초과했습니다. 인증번호를 재발송해주세요",
            )
        redis.setex(fail_key, _CODE_TTL, fail_count)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증번호가 일치하지 않습니다",
        )

    redis.delete(f"code:{email}")
    redis.delete(fail_key)
    redis.setex(f"verify:{email}", _CODE_TTL, "1")
    return {"message": "인증이 완료되었습니다"}


async def register_service(
    db: AsyncSession, redis: Redis, email: str, password: str
) -> dict[str, Any]:
    if not redis.get(f"verify:{email}"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이메일 인증이 필요합니다",
        )

    existing = await get_user_by_email(db, email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이미 가입된 이메일입니다",
        )

    user = await create_user(
        db,
        email=email,
        hashed_password=hash_password(password),
        provider="email",
    )
    redis.delete(f"verify:{email}")

    return {
        "access_token": create_access_token(str(user.id)),
        "refresh_token": create_refresh_token(str(user.id)),
        "token_type": "bearer",
    }


async def login_service(db: AsyncSession, email: str, password: str) -> dict[str, Any]:
    user = await get_user_by_email(db, email)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="가입되지 않은 이메일입니다. 회원가입을 진행해주세요",
        )

    if not user.hashed_password or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="이메일 또는 비밀번호가 올바르지 않습니다",
        )

    return {
        "access_token": create_access_token(str(user.id)),
        "refresh_token": create_refresh_token(str(user.id)),
        "token_type": "bearer",
    }


async def _fetch_kakao_user(code: str) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        token_data: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": settings.kakao_client_id,
            "redirect_uri": settings.kakao_redirect_uri,
            "code": code,
        }
        if settings.kakao_client_secret:
            token_data["client_secret"] = settings.kakao_client_secret

        token_res = await client.post(
            "https://kauth.kakao.com/oauth/token",
            data=token_data,
        )
        token_res.raise_for_status()
        kakao_token = token_res.json()["access_token"]

        info_res = await client.get(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {kakao_token}"},
        )
        info_res.raise_for_status()
        data = info_res.json()

    account = data.get("kakao_account", {})
    return {
        "provider_id": str(data["id"]),
        "email": account.get("email", ""),
    }


async def _fetch_google_user(code: str) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_res.raise_for_status()
        google_token = token_res.json()["access_token"]

        info_res = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {google_token}"},
        )
        info_res.raise_for_status()
        data = info_res.json()

    return {
        "provider_id": data["id"],
        "email": data.get("email", ""),
    }


async def oauth_callback_service(
    db: AsyncSession,
    provider: str,
    code: str,
) -> tuple[str, str, bool]:
    """Returns (access_token, refresh_token, is_new_user)."""
    if provider == "kakao":
        info = await _fetch_kakao_user(code)
    else:
        info = await _fetch_google_user(code)

    provider_id = info["provider_id"]
    email = info["email"]

    user = await get_user_by_provider(db, provider, provider_id)

    if not user and email:
        user = await get_user_by_email(db, email)

    if not user:
        user = await create_user(db, email=email, provider=provider, provider_id=provider_id)
    elif not user.provider_id:
        user = await update_user(db, user, provider=provider, provider_id=provider_id)

    is_new_user = not user.is_profile_set

    return (
        create_access_token(str(user.id)),
        create_refresh_token(str(user.id)),
        is_new_user,
    )


async def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    payload = decode_token(refresh_token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 refresh 토큰입니다")

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh 토큰이 아닙니다")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="유효하지 않은 토큰 페이로드입니다")

    # Rotation: 이전 refresh jti 블랙리스트 등록
    old_jti = payload.get("jti")
    exp = payload.get("exp")
    if old_jti and exp:
        remaining = exp - int(datetime.now(timezone.utc).timestamp())
        add_to_blacklist(old_jti, remaining)

    return {
        "access_token": create_access_token(user_id),
        "refresh_token": create_refresh_token(user_id),
        "token_type": "bearer",
    }


async def logout(access_token: str, refresh_token: str | None = None) -> None:
    now = int(datetime.now(timezone.utc).timestamp())

    # access 블랙리스트 (블랙리스트 체크 없이 디코드 — 이미 무효화됐을 수 있으므로)
    payload = _decode_without_blacklist_check(access_token)
    if payload:
        jti = payload.get("jti")
        exp = payload.get("exp", 0)
        if jti:
            add_to_blacklist(jti, max(exp - now, 0))

    # refresh 블랙리스트 (선택)
    if refresh_token:
        payload = _decode_without_blacklist_check(refresh_token)
        if payload:
            jti = payload.get("jti")
            exp = payload.get("exp", 0)
            if jti:
                add_to_blacklist(jti, max(exp - now, 0))


async def create_profile_service(
    db: AsyncSession, user: User, data: dict[str, Any]
) -> dict[str, Any]:
    await update_user(
        db,
        user,
        name=data.get("name"),
        gender=data.get("gender"),
        age=data.get("age"),
        role=data.get("role"),
        purposes=data.get("purposes"),
        purpose_custom=data.get("purpose_custom"),
        is_profile_set=True,
    )
    return {"message": "프로필이 저장되었습니다. 서비스를 시작합니다"}
