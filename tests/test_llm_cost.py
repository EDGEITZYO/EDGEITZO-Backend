"""LLM 비용 집계 — 프롬프트 캐시 토큰도 청구되므로 예산 카운터에 들어가야 한다."""
from app.services.llm.client import _calc_cost_micro_usd


def test_plain_call_cost_unchanged():
    # Sonnet 5: 입력 $2 / 출력 $10 per 1M
    assert _calc_cost_micro_usd("claude-sonnet-5", 1_000, 200) == 2_000 + 2_000


def test_cache_tokens_are_counted():
    """선정 사유 호출 실측값: 입력 750, 출력 190, 캐시 쓰기 1,365 (식은 캐시)."""
    cold = _calc_cost_micro_usd("claude-sonnet-5", 750, 190, cache_write_tokens=1_365)
    warm = _calc_cost_micro_usd("claude-sonnet-5", 750, 190, cache_read_tokens=1_365)
    plain = _calc_cost_micro_usd("claude-sonnet-5", 750, 190)
    assert cold == plain + int(1_365 * 2 * 1.25)
    assert warm == plain + int(1_365 * 2 * 0.1)
