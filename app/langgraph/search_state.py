from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict


class KeywordCandidate(TypedDict):
    """node_keyword_extractor 내부 처리 전용 — SearchState(세션 영속)에는 저장 안 함."""
    ko: str
    en: str
    desc: str


class FilterState(TypedDict):
    """연도/논문유형/인용수 3축 고정 + 누적 키워드"""
    pub_year_start: Optional[int]
    paper_type: Optional[str]  # DBCode 원본값 (JAKO/DIKO/JAFO/CFKO)
    citation_min: Optional[int]
    keywords: List[str]


class RefinementStep(TypedDict):
    """탐색 경로(history) 한 스텝"""
    step_type: str  # "search" | "narrow" | "expand"
    applied_filter: Optional[Dict[str, Any]]
    added_keyword: Optional[str]
    result_count: int
    timestamp: str


class NarrowChip(TypedDict):
    chip_id: str
    chip_type: str  # "year" | "paper_type" | "citation" — 3축 고정
    label: str  # 문구 템플릿 미정, 자리만
    value: Dict[str, Any]


class ExpandChip(TypedDict):
    chip_id: str
    chip_type: str  # "expand"
    keyword: str
    label: str
    co_occurrence_count: int


class SearchState(TypedDict):
    user_query: str
    session_id: str
    user_id: str
    sort_order: str  # "relevance" | "year_desc" | "citation_desc" 등, 유지되는 값
    research_purpose_class: Optional[str]  # "recency" | "citation" | "neutral" (정규식 분류 결과)
    filters: FilterState  # 누적 조건
    history: List[RefinementStep]  # 탐색 경로
    result_items: List[Dict[str, Any]]  # 최신 검색 결과 캐시
    total_count: int
    type_distribution: Dict[str, int]
    narrow_chips: List[NarrowChip]
    expand_chips: List[ExpandChip]
    ai_summary: Optional[str]
    summary_failed: bool  # 부분 실패 표시용
    fallback: Optional[str]  # "clarify" | "no_result" | "topic_change" | None
    is_broad_result: bool  # settings.search_broad_result_threshold 기준 판정 (임계값 미정 동안 항상 False)
    messages: List[Dict[str, Any]]


def empty_filters() -> FilterState:
    return FilterState(
        pub_year_start=None,
        paper_type=None,
        citation_min=None,
        keywords=[],
    )


def _apply_filter_update(filters: FilterState, updates: Dict[str, Any]) -> FilterState:
    """pub_year_start/paper_type/citation_min 중 None이 아닌 값만 반영한 새 filters 반환."""
    new_filters = dict(filters)
    for key in ("pub_year_start", "paper_type", "citation_min"):
        if updates.get(key) is not None:
            new_filters[key] = updates[key]
    return FilterState(**new_filters)


def _apply_keyword_addition(filters: FilterState, keyword: str) -> FilterState:
    """중복 아니면 filters['keywords']에 추가한 새 filters 반환."""
    existing = list(filters.get("keywords") or [])
    if keyword not in existing:
        existing.append(keyword)
    return FilterState(**{**filters, "keywords": existing})
