from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import bearer_scheme, decode_token
from app.core.settings import settings
from app.models.user import User
from app.repositories.user_repository import get_user_by_id


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = request.cookies.get("access_token") or (
        credentials.credentials if credentials else None
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다",
        )
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="토큰이 만료되었거나 유효하지 않습니다",
        )
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="토큰이 만료되었거나 유효하지 않습니다",
        )
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다",
        )
    return user


async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """로그인 안 해도 되는 엔드포인트에서 '로그인했으면 개인화'용으로 쓰는 옵셔널 인증.
    토큰이 없거나 유효하지 않으면 예외 없이 None 반환."""
    token = request.cookies.get("access_token") or (
        credentials.credentials if credentials else None
    )
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    return await get_user_by_id(db, user_id)


async def get_current_member(current_user: User = Depends(get_current_user)) -> User:
    """계정 관리 API(프로필 설정·수정, 탈퇴)용 — 게스트는 403."""
    if current_user.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="게스트 계정에서는 사용할 수 없는 기능입니다",
        )
    return current_user


async def require_demo_mode() -> None:
    """DEMO_MODE=false면 게스트 관련 엔드포인트를 없는 것처럼 404로 응답."""
    if not settings.demo_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
