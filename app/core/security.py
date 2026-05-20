import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi.security import HTTPBearer
from jwt.exceptions import InvalidTokenError
from redis import Redis

from app.core.settings import settings

bearer_scheme = HTTPBearer(auto_error=False)

_blacklist_redis: Optional[Redis] = None


def get_blacklist_redis() -> Redis:
    global _blacklist_redis
    if _blacklist_redis is None:
        _blacklist_redis = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_blacklist_db,
            password=settings.redis_password or None,
            decode_responses=True,
        )
    return _blacklist_redis


def is_token_blacklisted(jti: str) -> bool:
    try:
        return bool(get_blacklist_redis().exists(f"blacklist:{jti}"))
    except Exception:
        return False  # Redis 장애 시 통과 (운영 안정성 우선)


def add_to_blacklist(jti: str, expires_in_seconds: int) -> None:
    if expires_in_seconds <= 0:
        return
    try:
        get_blacklist_redis().setex(f"blacklist:{jti}", expires_in_seconds, "1")
    except Exception:
        pass


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_expire_minutes)
    return jwt.encode(
        {"sub": user_id, "exp": expire, "type": "access", "jti": str(uuid.uuid4())},
        settings.jwt_secret_key,
        algorithm="HS256",
    )


def create_refresh_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days)
    return jwt.encode(
        {"sub": user_id, "exp": expire, "type": "refresh", "jti": str(uuid.uuid4())},
        settings.jwt_secret_key,
        algorithm="HS256",
    )


def decode_token(token: str) -> Optional[dict]:
    """토큰 서명 검증 + 블랙리스트 체크. 실패 시 None 반환."""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
    except InvalidTokenError:
        return None

    jti = payload.get("jti")
    if jti and is_token_blacklisted(jti):
        return None

    return payload


def _decode_without_blacklist_check(token: str) -> Optional[dict]:
    """블랙리스트 체크 없이 디코드. 로그아웃 시 이미 무효화된 토큰 처리용."""
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
    except InvalidTokenError:
        return None
