from __future__ import annotations

from typing import Any, Dict, List, Optional

from typing_extensions import TypedDict


class SlotState(TypedDict):
    initial_query: bool
    research_purpose: Optional[str]
    paper_scope: Optional[str]
    keywords: Optional[List[str]]
    pub_year_range: Optional[str]


class KeywordCandidate(TypedDict):
    ko: str
    en: str
    desc: str


class SearchPreview(TypedDict):
    topic: Optional[str]
    purpose: Optional[str]
    scope: Optional[str]
    pub_year: Optional[str]
    keywords: List[str]
    completeness_pct: int


class SearchParams(TypedDict):
    keywords: List[str]
    scope: str
    pub_year_start: Optional[int]
    research_purpose: str
    trust_level: Optional[str]
    advanced_filters: Dict[str, Any]


class SearchState(TypedDict):
    user_query: str
    session_id: str
    user_id: str
    slots: SlotState
    keyword_candidates: Optional[List[KeywordCandidate]]
    advanced_filters: Dict[str, Any]
    messages: List[Dict[str, Any]]
    question_count: int
    search_preview: SearchPreview
    final_search_params: Optional[SearchParams]
    search_ready: bool
    # 응답 빌더 출력 (그래프 내부 전달용)
    ai_message: str
    options: List[Dict[str, Any]]
    allow_multiple: bool


def calc_completeness(slots: SlotState) -> int:
    pct = 0
    if slots.get("initial_query"):
        pct += 10
    if slots.get("research_purpose"):
        pct += 25
    if slots.get("paper_scope"):
        pct += 25
    if slots.get("keywords"):
        pct += 30
    if slots.get("pub_year_range") and slots["pub_year_range"] != "SKIP":
        pct += 10
    return pct


def empty_slots() -> SlotState:
    return SlotState(
        initial_query=False,
        research_purpose=None,
        paper_scope=None,
        keywords=None,
        pub_year_range=None,
    )


def build_search_preview(slots: SlotState, user_query: str) -> SearchPreview:
    return SearchPreview(
        topic=user_query or None,
        purpose=slots.get("research_purpose"),
        scope=slots.get("paper_scope"),
        pub_year=slots.get("pub_year_range"),
        keywords=slots.get("keywords") or [],
        completeness_pct=calc_completeness(slots),
    )
