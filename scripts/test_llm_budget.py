"""LLM 비용 한도 시뮬레이션 — 실제 API 호출 없음"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for _line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from app.core.redis import get_redis
from app.services.llm.client import (
    LLMBudgetExceededError,
    LLMResponse,
    _monthly_key,
    _DB,
    _cache_key,
    get_remaining_budget,
    get_monthly_cost,
)

_MODEL = "claude-haiku-4-5"
_MESSAGES = [{"role": "user", "content": "budget test"}]


def _reset_cost(r, value: float = 0.0) -> None:
    """차단 기준은 이번 달 카운터다 — 평생 누적이 아니라 이쪽을 세팅해야 한다."""
    r.set(_monthly_key(), int(value * 1_000_000))


async def test_budget_exceeded() -> bool:
    """이번 달 30.0 USD 세팅 → chat() 호출 시 LLMBudgetExceededError 발생 확인"""
    r = get_redis(_DB)
    _reset_cost(r, 30.0)

    from app.services.llm.client import chat
    try:
        await chat(messages=_MESSAGES, model=_MODEL, use_cache=False)
        print("FAIL: BudgetExceededError 미발생")
        return False
    except LLMBudgetExceededError as e:
        print(f"PASS [한도 초과 차단]: {e}")
        return True


async def test_cache_hit() -> bool:
    """캐시에 응답 직접 세팅 → chat() 호출 시 cached=True 반환 확인"""
    r = get_redis(_DB)
    _reset_cost(r, 0.0)

    key = _cache_key(_MODEL, _MESSAGES, 0.7, 1000)
    cached_payload = {
        "text": "cached response",
        "model": _MODEL,
        "input_tokens": 10,
        "output_tokens": 20,
        "cost_usd": 0.0,
    }
    r.set(key, json.dumps(cached_payload), ex=86400)

    from app.services.llm.client import chat
    resp = await chat(messages=_MESSAGES, model=_MODEL, use_cache=True)

    if resp.cached and resp.text == "cached response":
        print(f"PASS [캐시 hit]: text='{resp.text}', cost=${resp.cost_usd}")
        return True
    print(f"FAIL: cached={resp.cached}, text={resp.text}")
    return False


async def test_reset_and_budget() -> bool:
    """0으로 리셋 후 이번 달 사용액/잔여 예산 확인"""
    r = get_redis(_DB)
    _reset_cost(r, 0.0)

    total = await get_monthly_cost()
    remaining = await get_remaining_budget()

    ok = total == 0.0
    status = "PASS" if ok else "FAIL"
    print(f"{status} [리셋]: 이번달=${total:.4f}, 잔여=${remaining:.4f}")
    return ok


async def main() -> None:
    print("=== LLM 비용 한도 시뮬레이션 ===\n")
    results = [
        await test_budget_exceeded(),
        await test_cache_hit(),
        await test_reset_and_budget(),
    ]
    passed = sum(results)
    print(f"\n결과: {passed}/{len(results)} 통과")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
