"""AI 자연어 검색(채팅) 인당 이용 한도 — 공모전 투표 기간 LLM 비용 관리용.

한 사람이 쓸 수 있는 양을 두 단위로 나눈다.
  - 새 채팅 : 사용자별 누적 (settings.ai_new_chat_limit)
  - 턴     : 채팅(session_id)별 누적 (settings.ai_turns_per_chat_limit) — 메시지·칩으로 좁히기/확장하는 요청.
             정렬만 바꾸는 요청은 세지 않는다.
실측 비용(선정 사유 1회 생성 기준): 새 채팅 약 $0.043, 확장 턴 약 $0.045, 좁히기 턴 $0.002~0.02.

사용자 구분: 로그인(회원·게스트) 토큰의 user_id, 토큰이 없으면 IP.
카운트는 요청을 받는 순간 올리고, 검색이 실패하면 돌려준다(release) — 실패한 요청으로 횟수가
깎이지 않게. Redis 장애 시에는 통과시킨다(기존 rate limit과 같은 정책, 최종 방어선은 LLM 예산 가드).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Optional

from fastapi import Request, status

from app.core.exceptions import AppHTTPException
from app.core.rate_limit import get_client_ip
from app.core.redis import get_redis
from app.core.settings import settings

logger = logging.getLogger(__name__)

AI_CHAT_LIMIT = "AI_CHAT_LIMIT"
AI_TURN_LIMIT = "AI_TURN_LIMIT"

UsageKind = Literal["new_chat", "turn", "free"]


@dataclass
class AiUsage:
    """응답에 싣는 남은 횟수. 한도가 꺼져 있거나 Redis 장애면 None."""

    remaining_new_chats: Optional[int] = None
    remaining_turns: Optional[int] = None
    _consumed: list[str] = field(default_factory=list)


def usage_subject(request: Request, user_id: Optional[str]) -> str:
    return f"user:{user_id}" if user_id else f"ip:{get_client_ip(request)}"


def _new_chat_key(subject: str) -> str:
    return f"ai:new_chat:{subject}"


def _turn_key(subject: str, session_id: str) -> str:
    return f"ai:turn:{subject}:{session_id}"


def _count(r, key: str) -> int:
    return int(r.get(key) or 0)


def _consume(r, key: str, limit: int) -> Optional[int]:
    """1 올리고 사용 후 횟수를 돌려준다. 한도를 넘으면 되돌리고 None."""
    pipe = r.pipeline(transaction=True)
    pipe.set(key, 0, ex=settings.ai_usage_window_seconds, nx=True)
    pipe.incr(key)
    _, count = pipe.execute()
    if count > limit:
        r.decr(key)
        return None
    return count


def consume_ai_usage(subject: str, session_id: str, kind: UsageKind) -> AiUsage:
    """kind에 해당하는 횟수를 1 쓰고 남은 횟수를 돌려준다. 한도를 넘었으면 429(AppHTTPException)."""
    if not settings.ai_usage_limit_enabled:
        return AiUsage()

    chat_limit, turn_limit = settings.ai_new_chat_limit, settings.ai_turns_per_chat_limit
    new_key, turn_key = _new_chat_key(subject), _turn_key(subject, session_id)
    try:
        r = get_redis(settings.redis_rate_limit_db)
        consumed: list[str] = []
        if kind == "new_chat":
            used = _consume(r, new_key, chat_limit)
            if used is None:
                raise AppHTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"AI 검색은 1인당 {chat_limit}회까지 이용할 수 있어요.",
                    error_code=AI_CHAT_LIMIT,
                )
            consumed.append(new_key)
            return AiUsage(chat_limit - used, turn_limit, consumed)

        if kind == "turn":
            used = _consume(r, turn_key, turn_limit)
            if used is None:
                raise AppHTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"한 검색에서 좁히기·확장은 {turn_limit}회까지 할 수 있어요.",
                    error_code=AI_TURN_LIMIT,
                )
            consumed.append(turn_key)
            return AiUsage(max(0, chat_limit - _count(r, new_key)), turn_limit - used, consumed)

        return AiUsage(max(0, chat_limit - _count(r, new_key)), max(0, turn_limit - _count(r, turn_key)))
    except AppHTTPException:
        raise
    except Exception:
        logger.warning("AI 이용 한도 Redis 오류 — 통과시킴 subject=%s", subject, exc_info=True)
        return AiUsage()


def release_ai_usage(usage: AiUsage) -> None:
    """검색이 실패했을 때 쓴 횟수를 돌려준다."""
    if not usage._consumed:
        return
    try:
        r = get_redis(settings.redis_rate_limit_db)
        for key in usage._consumed:
            r.decr(key)
    except Exception:
        logger.warning("AI 이용 한도 반환 실패", exc_info=True)
