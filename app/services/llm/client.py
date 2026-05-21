import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date

from app.core.redis import get_redis
from app.core.settings import settings

logger = logging.getLogger(__name__)

_DB = 6
_CACHE_TTL = 86400  # 24시간

_COST_KEY_TOTAL = "llm:cost:total:edgeitzo"
_COST_KEY_DAILY_PREFIX = "llm:cost:daily"

# 모델별 단가 (USD per 1M tokens) — 확정 모델 결정 후 갱신
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
}

_BUDGET_MICRO_USD = int(settings.llm_budget_total_usd * 1_000_000)


class LLMBudgetExceededError(Exception):
    """누적 비용이 한도에 도달했을 때 발생 — API 호출 자체를 차단"""


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool


def _cache_key(model: str, messages: list[dict], temperature: float, max_tokens: int) -> str:
    payload = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
        sort_keys=True,
    )
    return f"llm:{model}:{hashlib.sha256(payload.encode()).hexdigest()}"


def _calc_cost_micro_usd(model: str, input_tokens: int, output_tokens: int) -> int:
    pricing = _PRICING.get(model, {"input": 3.0, "output": 15.0})
    cost_usd = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    return int(cost_usd * 1_000_000)


async def _call_claude(
    messages: list[dict], model: str, temperature: float, max_tokens: int
) -> tuple[str, int, int]:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature, messages=messages
    )
    return resp.content[0].text, resp.usage.input_tokens, resp.usage.output_tokens


async def _call_openai(
    messages: list[dict], model: str, temperature: float, max_tokens: int
) -> tuple[str, int, int]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
    )
    usage = resp.usage
    return resp.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens


async def chat(
    messages: list[dict],
    model: str,
    temperature: float = 0.7,
    max_tokens: int = 1000,
    use_cache: bool = True,
) -> LLMResponse:
    """LLM 호출 — 비용 한도 체크 → 캐시 조회 → API 호출 → 비용 누적 → 캐시 저장"""
    r = get_redis(_DB)
    cache_key = _cache_key(model, messages, temperature, max_tokens)

    # 1. 비용 한도 체크 (하드 차단)
    current = int(r.get(_COST_KEY_TOTAL) or 0)
    if current >= _BUDGET_MICRO_USD:
        raise LLMBudgetExceededError(
            f"누적 비용 한도 초과: ${current / 1_000_000:.4f} / ${settings.llm_budget_total_usd}"
        )

    # 2. 캐시 조회
    if use_cache:
        hit = r.get(cache_key)
        if hit:
            return LLMResponse(cached=True, **json.loads(hit))

    # 3. API 호출
    if model.startswith("claude"):
        text, input_tokens, output_tokens = await _call_claude(messages, model, temperature, max_tokens)
    else:
        text, input_tokens, output_tokens = await _call_openai(messages, model, temperature, max_tokens)

    # 4. 비용 계산 + 누적 (실패해도 호출 결과는 반환)
    cost_micro = _calc_cost_micro_usd(model, input_tokens, output_tokens)
    try:
        r.incrby(_COST_KEY_TOTAL, cost_micro)
        daily_key = f"{_COST_KEY_DAILY_PREFIX}:{date.today().isoformat()}"
        r.incrby(daily_key, cost_micro)
        r.expire(daily_key, 86400 * 7)
    except Exception:
        logger.warning("비용 누적 실패 (호출은 성공)", exc_info=True)

    payload = {
        "text": text,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_micro / 1_000_000,
    }

    # 5. 캐시 저장
    if use_cache:
        r.set(cache_key, json.dumps(payload), ex=_CACHE_TTL)

    return LLMResponse(cached=False, **payload)


async def get_total_cost() -> float:
    """누적 비용 조회 (USD)"""
    return int(get_redis(_DB).get(_COST_KEY_TOTAL) or 0) / 1_000_000


async def get_remaining_budget() -> float:
    """잔여 예산 조회 (USD)"""
    return settings.llm_budget_total_usd - await get_total_cost()
