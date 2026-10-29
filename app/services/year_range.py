"""논문 목록 필터의 발행연도 범위 — 모든 목록 API가 같은 규칙을 쓰도록 한곳에 둔다.

범위는 양 끝 포함(2023~2025 → 2023·2024·2025년). 한쪽 끝이 None이면 그쪽은 열려 있다.
연도를 모르는 논문은 범위가 하나라도 걸리면 빠진다 — 예전 단일 연도 필터와 같은 동작.

예전 API는 연도 하나(year)만 받아 "그 해만" 걸렀다. 프런트가 범위로 옮겨오기 전까지
깨지지 않도록 year는 (year, year) 범위로 받아준다. 범위 값이 함께 오면 범위가 우선.
"""
from __future__ import annotations

from typing import Iterable, Optional

YearRange = tuple[Optional[int], Optional[int]]


def resolve_year_range(
    year: Optional[int] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
) -> YearRange:
    if start is None and end is None and year is not None:
        return year, year
    if start is not None and end is not None and start > end:
        return end, start
    return start, end


def year_bounds(years: Iterable[Optional[int]]) -> YearRange:
    """목록에서 가장 오래된·가장 최근 발행 연도. 연도를 아는 논문이 없으면 (None, None)."""
    known = [y for y in years if y is not None]
    return (min(known), max(known)) if known else (None, None)


def year_in_range(year: Optional[int], start: Optional[int], end: Optional[int]) -> bool:
    if start is None and end is None:
        return True
    if year is None:
        return False
    return (start is None or year >= start) and (end is None or year <= end)
