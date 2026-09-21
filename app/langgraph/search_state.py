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
    # 발행 연도 — 정확히 그 해만 매칭(범위 아님). 경로(LLM/칩/드롭다운)와 무관하게 의미가 하나다.
    # 와이어 필드명은 프런트 호환 때문에 유지 중이나 "start"는 더 이상 범위를 뜻하지 않는다.
    pub_year_start: Optional[int]
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


class KeywordMapAnchor(TypedDict):
    """키워드맵 화면 중앙에 고정할 노드. 검색 결과 논문들의 원본 키워드에서 뽑으므로
    Neo4j에 반드시 존재한다 — 사용자 문장이나 LLM 키워드로 다시 찾을 필요가 없다."""
    key: str
    name_ko: Optional[str]
    name_en: Optional[str]
    paper_count: int


class SearchState(TypedDict):
    user_query: str
    session_id: str
    user_id: str
    sort_order: str  # "relevance" | "year_desc" | "citation_desc" 등, 유지되는 값
    research_purpose_class: Optional[str]  # "recency" | "citation" | "neutral" (정규식 분류 결과)
    filters: FilterState  # 누적 조건
    # 논문 목록 패널(드롭다운·토글)이 직접 건 필터 필드명. 패널이 null을 보냈을 때
    # '해제'로 볼지 '무시'로 볼지 가르는 유일한 근거다 — 여기 있으면 패널이 자기가 건 걸
    # 되돌리는 것이므로 해제하고, 없으면 채팅(자연어·칩)이 건 것이므로 건드리지 않는다.
    # 프런트는 드롭다운의 현재값을 매 요청 보내는데, 그 드롭다운은 채팅이 건 필터를 모른 채
    # 계속 '전체'(null)로 남아 있다. 이 구분이 없으면 그 null이 매 턴 채팅 필터를 지워
    # "필터를 하나 더 걸었는데 결과가 늘어나는" 일이 생긴다(실측: 석사 16건 → KCI 추가 시 69건).
    panel_owned: List[str]
    history: List[RefinementStep]  # 탐색 경로
    result_items: List[Dict[str, Any]]  # 최신 검색 결과 캐시
    total_count: int
    type_distribution: Dict[str, int]
    narrow_chips: List[NarrowChip]
    expand_chips: List[ExpandChip]
    keyword_map_anchor: Optional[KeywordMapAnchor]  # 키워드맵 앵커. 결과가 없거나 못 찾으면 None
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
        paper_type=None,
        citation_min=None,
        kci_only=None,
        sci_only=None,
        keywords=[],
    )


def _apply_filter_update(filters: FilterState, updates: Dict[str, Any]) -> FilterState:
    """pub_year_start/paper_type/citation_min/kci_only/sci_only 중 None이 아닌 값만 반영한 새 filters 반환."""
    new_filters = dict(filters)
    for key in ("pub_year_start", "paper_type", "citation_min", "kci_only", "sci_only"):
        if updates.get(key) is not None:
            new_filters[key] = updates[key]
    return FilterState(**new_filters)


def _release_panel_ownership(panel_owned: List[str], updates: Dict[str, Any]) -> List[str]:
    """채팅(LLM 분류/칩)이 값을 지정한 필드를 패널 소유에서 뗀다.

    패널이 연도를 걸어둔 상태에서 사용자가 채팅으로 다른 연도를 말하면, 그 필드의 주인은
    채팅으로 넘어간다. 그래야 이후 패널이 보내는 null이 채팅이 방금 건 값을 지우지 않는다.
    _apply_filter_update와 항상 같이 호출할 것."""
    return [f for f in panel_owned if updates.get(f) is None]


def _apply_keyword_addition(filters: FilterState, keyword: str) -> FilterState:
    """중복 아니면 filters['keywords']에 추가한 새 filters 반환."""
    existing = list(filters.get("keywords") or [])
    if keyword not in existing:
        existing.append(keyword)
    return FilterState(**{**filters, "keywords": existing})
