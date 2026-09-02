"""LLM 비용 카운터의 기간 경계 테스트.

예전에는 평생 누적으로 차단해서, 한 번 상한을 넘으면 사람이 리셋 API를 부르기 전까지
모든 LLM 기능이 영구히 죽었다. 실제 과금은 달마다 다시 계산되므로 이번 달 예산이 멀쩡히
남아 있어도 막히는 상태였다. 이제는 달이 바뀌면 키가 바뀌어 저절로 회복된다.

Redis가 필요한 누적/차단 동작은 scripts/test_llm_budget.py가 통합으로 확인한다.
여기서는 Redis 없이 검증할 수 있는 "기간이 언제 넘어가는가"만 본다 — 12월→1월처럼
조용히 틀리기 쉬운 곳이다.
"""
from datetime import date

from app.services.llm.client import _monthly_key, next_reset_date


def test_같은_달은_같은_키를_쓴다():
    assert _monthly_key(date(2026, 9, 1)) == _monthly_key(date(2026, 9, 30))


def test_달이_바뀌면_키가_바뀐다():
    """키가 바뀌는 것이 곧 자동 회복이다 — 새 키는 0에서 시작한다."""
    assert _monthly_key(date(2026, 9, 30)) != _monthly_key(date(2026, 10, 1))


def test_연도가_달라도_같은_월과_섞이지_않는다():
    assert _monthly_key(date(2026, 9, 3)) != _monthly_key(date(2027, 9, 3))


def test_리셋일은_다음달_1일():
    assert next_reset_date(date(2026, 9, 3)) == date(2026, 10, 1)
    assert next_reset_date(date(2026, 9, 30)) == date(2026, 10, 1)


def test_12월은_다음해_1월로_넘어간다():
    """% 연산으로 월을 굴릴 때 연도 올림을 빠뜨리기 쉬운 지점."""
    assert next_reset_date(date(2026, 12, 15)) == date(2027, 1, 1)


def test_말일이_28일인_달도_다음달_1일():
    assert next_reset_date(date(2026, 2, 28)) == date(2026, 3, 1)


def test_리셋일은_항상_오늘보다_뒤다():
    """매달 1일·말일을 포함해 한 해를 전부 훑는다."""
    for month in range(1, 13):
        for day in (1, 15, 28):
            d = date(2026, month, day)
            assert next_reset_date(d) > d, d
