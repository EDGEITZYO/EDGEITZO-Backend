from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict


class KeywordCandidate(TypedDict):
    """node_keyword_extractor 내부 처리 전용 — SearchState(세션 영속)에는 저장 안 함."""
    ko: str
    en: str
    desc: str


class FilterState(TypedDict):
    """연도/논문유형/인용수/KCI/SCI 5축 고정 + 누적 키워드"""
    pub_year_start: Optional[int]
    pub_year_exact: Optional[bool]  # true면 pub_year_start를 "그 연도 이상"이 아니라 "정확히 그 해"로 매칭
    paper_type: Optional[str]  # "학술 저널" | "박사학위 논문" | "석사학위 논문" (사용자 노출 레이블)
    citation_min: Optional[int]
    kci_only: Optional[bool]  # true면 KCI 등재만
    sci_only: Optional[bool]  # true면 SCI 계열(SCIE/SSCI/AHCI) 등재만
    keywords: List[str]


class RefinementStep(TypedDict):
    """탐색 경로(history) 한 스텝. result_items는 이 스텝 시점의 검색 결과 스냅샷 —
    프론트가 이전 턴의 '논문 보기' 버튼을 눌렀을 때 재검색 없이 그대로 보여줄 수 있도록
    스텝별로 독립 저장한다(SearchState.result_items는 최신 턴 것만 남는 것과 별개)."""
    step_id: str
    step_type: str  # "search" | "narrow" | "expand"
    applied_filter: Optional[Dict[str, Any]]
    added_keyword: Optional[str]
    result_count: int
    result_items: List[Dict[str, Any]]
    timestamp: str


class NarrowChip(TypedDict):
    chip_id: str
    chip_type: str  # "year" | "paper_type" | "citation" — 3축 고정
    label: str  # 사용자 노출용 문구. 템플릿은 search_graph.py의 _build_narrow_chips 참고
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
    _free_input_intent: Optional[str]  # free_input_classifier→response_builder 전달용 임시 신호.
    _skip_classification: Optional[bool]  # 칩 클릭/필터 직접 지정(자유입력 없음) 시 True.
    # response_builder가 이번 턴엔 새 사용자 발화가 없었음을 알고 ai_summary 재생성(LLM 호출)을
    # 건너뛸 수 있게 하는 신호로도 쓰인다.
    # LangGraph는 노드 간 엣지 전달 시 StateGraph(SearchState)에 선언된 채널만 유지하므로
    # (수신 함수의 파라미터 타입힌트와 무관), 스키마에 없으면 값이 전달 도중 사라진다.
    # 세션에 영속화하면 안 되므로 _save_state()에서 저장 직전 반드시 pop한다.


def empty_filters() -> FilterState:
    return FilterState(
        pub_year_start=None,
        pub_year_exact=None,
        paper_type=None,
        citation_min=None,
        kci_only=None,
        sci_only=None,
        keywords=[],
    )


def _apply_filter_update(filters: FilterState, updates: Dict[str, Any]) -> FilterState:
    """pub_year_start/paper_type/citation_min/kci_only/sci_only 중 None이 아닌 값만 반영한 새 filters 반환.
    이 경로(LLM 분류/칩 클릭)로 들어오는 pub_year_start는 항상 "이후" 범위 검색이므로,
    이전에 논문 목록 드롭다운(_apply_direct_filters)이 남겨둔 pub_year_exact=True가
    새 pub_year_start에 잘못 적용되지 않도록 함께 초기화한다."""
    new_filters = dict(filters)
    for key in ("pub_year_start", "paper_type", "citation_min", "kci_only", "sci_only"):
        if updates.get(key) is not None:
            new_filters[key] = updates[key]
            if key == "pub_year_start":
                new_filters["pub_year_exact"] = False
    return FilterState(**new_filters)


def _apply_keyword_addition(filters: FilterState, keyword: str) -> FilterState:
    """중복 아니면 filters['keywords']에 추가한 새 filters 반환."""
    existing = list(filters.get("keywords") or [])
    if keyword not in existing:
        existing.append(keyword)
    return FilterState(**{**filters, "keywords": existing})
