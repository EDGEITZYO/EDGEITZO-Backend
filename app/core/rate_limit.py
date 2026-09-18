"""데모 기간 남용 방지용 고정 윈도우 rate limit (Redis).

- 게스트 발급(POST /auth/guest): IP 기준
- 검색/LLM 호출 API: 게스트는 user_id 기준, 토큰 없는 요청은 IP 기준. 회원은 제한 없음.

DEMO_MODE=false면 검색/LLM 제한은 통과시킨다 — 플래그를 끄면 기존 동작과 같아야 하므로.
Redis 장애 시에도 통과시킨다(블랙리스트와 같은 정책). 비용의 최종 방어선은 LLM 월 예산 가드다.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials

from app.core.redis import get_redis
from app.core.security import bearer_scheme, decode_token
from app.core.settings import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "rl"


def get_client_ip(request: Request) -> str:
    """Nginx 뒤에서의 실제 클라이언트 IP.

    X-Forwarded-For는 "클라이언트가 보낸 값, ..., 프록시가 붙인 값" 순이라 맨 왼쪽은 조작 가능하다.
    신뢰하는 프록시 수(trusted_proxy_hops)만큼 오른쪽에서 센 값을 쓴다.
    """
    forwarded = request.headers.get("x-forwarded-for")
    hops = settings.trusted_proxy_hops
    if forwarded and hops > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-hops] if len(parts) >= hops else parts[0]
    return request.client.host if request.client else "unknown"


def _hit(key: str, limit: int, window_seconds: int) -> Optional[int]:
    """카운트를 1 올린다. 한도를 넘었으면 윈도우가 풀릴 때까지 남은 초, 아니면 None."""
    try:
        r = get_redis(settings.redis_rate_limit_db)
        pipe = r.pipeline(transaction=True)
        pipe.set(key, 0, ex=window_seconds, nx=True)
        pipe.incr(key)
        pipe.ttl(key)
        _, count, ttl = pipe.execute()
    except Exception:
        logger.warning("rate limit Redis 오류 — 통과시킴 key=%s", key, exc_info=True)
        return None
    if count > limit:
        return max(int(ttl), 1)
    return None


def _raise_too_many(retry_after: int, detail: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


def guest_issue_key(ip: str) -> str:
    return f"{_KEY_PREFIX}:guest_issue:{ip}"


def llm_guest_key(user_id: str) -> str:
    return f"{_KEY_PREFIX}:llm:guest:{user_id}"


def llm_ip_key(ip: str) -> str:
    return f"{_KEY_PREFIX}:llm:ip:{ip}"


async def limit_guest_issue(request: Request) -> None:
    retry_after = _hit(
        guest_issue_key(get_client_ip(request)),
        settings.guest_issue_limit_per_ip,
        settings.guest_issue_window_seconds,
    )
    if retry_after is not None:
        _raise_too_many(retry_after, "게스트 발급 요청이 너무 많습니다. 잠시 후 다시 시도해주세요")


async def limit_llm_calls(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    """검색/LLM 호출 API용. 토큰의 guest 클레임으로 게스트를 가린다(DB 조회 없음).
    만료·위조 토큰은 토큰 없음과 같이 IP 기준으로 센다."""
    if not settings.demo_mode:
        return

    token = request.cookies.get("access_token") or (credentials.credentials if credentials else None)
    payload = decode_token(token) if token else None
    if payload and payload.get("type") == "access" and payload.get("sub"):
        if not payload.get("guest"):
            return  # 회원은 제한하지 않는다
        key, limit = llm_guest_key(payload["sub"]), settings.guest_llm_rate_limit
    else:
        key, limit = llm_ip_key(get_client_ip(request)), settings.anon_llm_rate_limit_per_ip

    retry_after = _hit(key, limit, settings.llm_rate_window_seconds)
    if retry_after is not None:
        _raise_too_many(retry_after, "요청이 너무 많습니다. 잠시 후 다시 시도해주세요")
