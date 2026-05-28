from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone

from app.core.redis import get_redis

_DB = 4
_TTL = 3600  # 대화 비활성 1시간 후 자동 만료


def _key(session_id: str) -> str:
    return f"dialog:{session_id}"


def create_session(user_id: str) -> str:
    """새 대화 세션 생성, session_id(UUID) 반환"""
    session_id = str(uuid.uuid4())
    r = get_redis(_DB)
    r.hset(
        _key(session_id),
        mapping={
            "state": json.dumps({}),
            "turn_count": 0,
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    r.expire(_key(session_id), _TTL)
    return session_id


def get_state(session_id: str) -> dict | None:
    """세션 state 반환, 세션 없으면 None"""
    raw = get_redis(_DB).hget(_key(session_id), "state")
    return json.loads(raw) if raw is not None else None


def update_state(session_id: str, state: dict) -> None:
    # "state 갱신 + TTL 리셋 (매 turn 호출)
    r = get_redis(_DB)
    r.hset(_key(session_id), "state", json.dumps(state))
    r.expire(_key(session_id), _TTL)


def increment_turn(session_id: str) -> int:
    # turn_count 1 증가 후 현재 값 반환
    r = get_redis(_DB)
    count = r.hincrby(_key(session_id), "turn_count", 1)
    r.expire(_key(session_id), _TTL)
    return int(count)


def delete_session(session_id: str) -> None:
    # 세션 삭제
    get_redis(_DB).delete(_key(session_id))
