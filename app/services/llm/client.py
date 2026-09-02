import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Optional

from app.core.redis import get_redis
from app.core.settings import settings

logger = logging.getLogger(__name__)

_DB = 6
_CACHE_TTL = 86400  # 24시간

_COST_KEY_TOTAL = "llm:cost:total:edgeitzo"
_COST_KEY_DAILY_PREFIX = "llm:cost:daily"

_BUDGET_MICRO_USD = int(settings.llm_budget_total_usd * 1_000_000)


class LLMBudgetExceededError(Exception):
    """누적 비용이 한도에 도달했을 때 발생 — API 호출 자체를 차단"""

# 모델별 단가 (USD per 1M tokens) — 확정 모델 결정 후 갱신
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool


def _cache_key(
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    thinking: Optional[dict] = None,
) -> str:
    body: dict = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    # thinking 미지정 호출은 기존 캐시 키를 그대로 유지해야 하므로 지정됐을 때만 넣는다
    if thinking is not None:
        body["thinking"] = thinking
    payload = json.dumps(body, sort_keys=True)
    return f"llm:{model}:{hashlib.sha256(payload.encode()).hexdigest()}"


# 단가표에 없는 모델을 만났을 때 쓰는 값. Sonnet 4.5 단가라 대개 실제보다 비싸게 잡힌다 —
# 예산이 실제보다 빨리 닳는 쪽이라 과금 위험은 없지만, 조용히 틀리면 안 된다(아래 경고 참고).
_DEFAULT_PRICING = {"input": 3.0, "output": 15.0}


def _resolve_pricing(model: str) -> dict[str, float]:
    """모델 ID → 단가. 날짜 꼬리가 붙은 ID도 받아준다.

    예전에는 _PRICING.get(model, 기본값)으로 끝냈는데, .env에 날짜가 붙은
    'claude-haiku-4-5-20251001'이 들어 있어 표와 안 맞았다. 그래서 Haiku 호출이 전부
    기본 단가($3/$15)로 기록돼 실제(1/5)의 3배로 집계됐고, 아무 신호도 없어 한동안
    아무도 몰랐다. 접두로 흡수하고, 그래도 못 찾으면 반드시 경고를 남긴다.
    """
    if model in _PRICING:
        return _PRICING[model]
    for known, pricing in _PRICING.items():
        if model.startswith(known):
            return pricing
    logger.warning(
        "단가표에 없는 모델 '%s' — 기본 단가로 집계한다. 예산 카운터가 부정확해지므로 "
        "_PRICING에 추가할 것", model,
    )
    return _DEFAULT_PRICING


def _calc_cost_micro_usd(model: str, input_tokens: int, output_tokens: int) -> int:
    pricing = _resolve_pricing(model)
    cost_usd = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000
    return int(cost_usd * 1_000_000)


# 샘플링 파라미터는 두 겹으로 막혀 있어 둘 다 다뤄야 한다.
#
# 1) 모델: Sonnet 5 이상(Opus 5, Fable 5 등)은 temperature/top_p/top_k를 거부한다(400).
# 2) SDK: anthropic 1.2.0이 messages.create() 시그니처에서 이 셋을 아예 제거했다.
#    그래서 이름있는 인자로 넘기면 모델과 무관하게 TypeError가 난다 —
#    "AsyncMessages.create() got an unexpected keyword argument 'temperature'".
#    Haiku 4.5는 API가 여전히 temperature를 받으므로, 값을 유지하려면 extra_body로 보내야 한다.
#
# 이 구분이 중요한 이유: 예전엔 모델 목록만 맞으면 됐지만, 이제 목록에 없는 모델(haiku 등)도
# 이름있는 인자로는 못 넘긴다. 실제로 이것 때문에 키워드 추출이 전량 실패해 검색이
# 질의와 무관한 결과를 돌려줬다.
#
# 단가표와 같은 이유로 접두 비교한다: 날짜 꼬리가 붙은 ID면 정확 일치가 빗나간다.
_NO_SAMPLING_PARAMS_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-mythos-5")


def _rejects_sampling_params(model: str) -> bool:
    return model.startswith(_NO_SAMPLING_PARAMS_MODELS)


@lru_cache(maxsize=1)
def _client():
    """AsyncAnthropic 하나를 재사용한다.

    예전에는 호출마다 새로 만들었다. 선정 사유는 한 검색에 동시 호출이 10~20개씩 나가는데,
    그때마다 클라이언트가 새로 생기면 커넥션 풀도 매번 새로 생겨 TCP·TLS 핸드셰이크를
    처음부터 다시 한다. 연결 오류가 늘어나는 것도 여기서 온다.

    max_retries를 명시하는 이유: SDK가 429·5xx·연결 오류를 자체 백오프로 재시도해 주는데,
    그 기본값(2)에 기대면 SDK 버전이 바뀔 때 조용히 달라진다. anthropic은 requirements.txt에
    핀이 없어 배포마다 최신이 깔린다 — 실제로 1.2.0에서 temperature 인자가 사라져 검색이
    통째로 죽은 적이 있다(b0d9173). 기본값에 의존하지 않는다.
    """
    import anthropic
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def is_retryable(exc: BaseException) -> bool:
    """이 예외를 다시 시도해 볼 만한가.

    호출부(선정 사유 등)가 anthropic을 직접 import하지 않고도 재시도를 판단할 수 있게
    여기서 answer한다 — 예외 타입을 아는 곳은 이 모듈뿐이다.

    SDK가 이미 429·5xx·연결 오류를 재시도한 뒤라 여기까지 온 건 "SDK 재시도까지 소진"을
    뜻한다. 그래도 초 단위로 띄워 다시 던지면 살아나는 경우가 있어 재시도 대상으로 둔다.
    반대로 400·401·403·404는 몇 번을 던져도 같은 답이 오므로 즉시 포기한다 —
    이건 우리 코드나 키가 잘못된 것이고, 재시도는 장애를 가릴 뿐이다.
    """
    import anthropic

    if isinstance(exc, LLMBudgetExceededError):
        return False  # 예산은 시간이 지나도 안 돌아온다. 사람이 올려줘야 한다.
    if isinstance(exc, (anthropic.BadRequestError, anthropic.AuthenticationError,
                        anthropic.PermissionDeniedError, anthropic.NotFoundError)):
        return False
    return True


async def _call_claude(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    thinking: Optional[dict] = None,
) -> tuple[str, int, int]:
    client = _client()
    # 이름있는 인자가 아니라 extra_body로 보낸다 (위 주석 2번 참고).
    sampling_kwargs = (
        {} if _rejects_sampling_params(model) else {"extra_body": {"temperature": temperature}}
    )
    # Sonnet 5는 thinking 기본값이 adaptive라, 1~2문장짜리 짧은 산출물에는 순수 오버헤드가 된다.
    # {"type": "disabled"}를 명시하면 모델 품질은 그대로 두고 사고 단계만 끌 수 있다.
    thinking_kwargs = {"thinking": thinking} if thinking is not None else {}
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, messages=messages,
        **sampling_kwargs, **thinking_kwargs,
    )
    if resp.stop_reason == "max_tokens":
        logger.warning(
            "LLM 응답이 max_tokens(%d)에 걸려 중간에 잘림 — model=%s output_tokens=%d",
            max_tokens, model, resp.usage.output_tokens,
        )
    # 위 모델들은 기본적으로 adaptive thinking이 켜져 있어 thinking 블록이 먼저 오므로
    # content[0]이 아니라 text 타입 블록을 명시적으로 찾아야 함
    text_block = next((b for b in resp.content if b.type == "text"), None)
    if text_block is None:
        raise ValueError(f"Claude 응답에 text 블록이 없음 (model={model}, stop_reason={resp.stop_reason})")
    return text_block.text, resp.usage.input_tokens, resp.usage.output_tokens


async def chat(
    messages: list[dict],
    model: str,
    temperature: float = 0.7,
    max_tokens: int = 1000,
    use_cache: bool = True,
    thinking: Optional[dict] = None,
) -> LLMResponse:
    """LLM 호출 — 비용 한도 체크 → 캐시 조회 → API 호출 → 비용 누적 → 캐시 저장

    thinking: None이면 모델 기본값(Sonnet 5는 adaptive). 짧은 산출물에는
    {"type": "disabled"}로 사고 단계를 꺼서 지연을 줄인다."""
    r = get_redis(_DB)
    cache_key = _cache_key(model, messages, temperature, max_tokens, thinking)

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
    text, input_tokens, output_tokens = await _call_claude(
        messages, model, temperature, max_tokens, thinking
    )

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


async def reset_cost() -> None:
    """누적 비용 카운터 초기화 — API 키 교체 시 호출"""
    r = get_redis(_DB)
    r.set(_COST_KEY_TOTAL, 0)
