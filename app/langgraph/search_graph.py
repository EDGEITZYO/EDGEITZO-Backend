from __future__ import annotations

import json
import logging
import math
from collections import Counter
from datetime import datetime, timezone
from typing import List, Optional

from kiwipiepy import Kiwi
from langgraph.graph import END, StateGraph

from app.constants.purpose_keywords import CITATION_KEYWORDS, RECENCY_KEYWORDS
from app.core.settings import settings
from app.langgraph.search_state import (
    ExpandChip,
    NarrowChip,
    RefinementStep,
    SearchState,
    _apply_filter_update,
    _apply_keyword_addition,
    empty_filters,
)
from app.services.chroma_search_service import get_chroma_search_service
from app.services.llm.client import chat

logger = logging.getLogger(__name__)

_MODEL = settings.llm_default_model
_kiwi = Kiwi()  # 모듈 로드 시 1회 초기화 (싱글턴)

_KEYWORD_SYSTEM = """학술 키워드 추출기. 사용자 입력에서 연구 키워드를 추출해 JSON만 반환. 절대 설명하지 말 것. JSON 외 텍스트 금지."""

_FREE_INPUT_SYSTEM = """검색 정교화 의도 분류기. 사용자 입력을 보고 JSON만 반환.
절대 설명하지 말 것. JSON 외 텍스트 금지."""


async def _llm_json(system: str, user: str, max_tokens: int = 500) -> dict:
    try:
        resp = await chat(
            messages=[
                {"role": "user", "content": f"[System]\n{system}\n\n[User]\n{user}"},
            ],
            model=_MODEL,
            temperature=0.0,
            max_tokens=max_tokens,
        )
        text = resp.text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning("LLM JSON 파싱 실패: %s", e)
        return {}


# ── node_intent_extractor: 최초 검색 전용, 정규식(형태소) 목적분류, LLM 없음 ──────

def _classify_research_purpose(user_query: str) -> str:
    """kiwipiepy로 명사(NNG/NNP) 추출 후 RECENCY/CITATION 키워드셋과 교집합 비교.
    동시 매칭 시 recency 우선. 매칭 없으면 중립."""
    tokens = _kiwi.tokenize(user_query)
    nouns = {t.form for t in tokens if t.tag in ("NNG", "NNP")}
    if nouns & set(RECENCY_KEYWORDS):
        return "recency"
    if nouns & set(CITATION_KEYWORDS):
        return "citation"
    return "neutral"


async def node_intent_extractor(state: SearchState) -> SearchState:
    """최초 검색 전용 — LLM 미사용, keyword_extractor로 무조건 진행."""
    user_query = state.get("user_query", "")
    purpose_class = _classify_research_purpose(user_query)
    return {**state, "research_purpose_class": purpose_class}


# ── node_keyword_extractor: 최초 검색 경로 전용, 기존 프롬프트 그대로 ──────────

async def node_keyword_extractor(state: SearchState) -> SearchState:
    """최초 검색 경로 전용. 프롬프트(불용어 제거→명사구 추출→학술 키워드 변환→상위 3개)는
    기존 그대로 — 신규 작성 금지 원칙 반영."""
    user_query = state.get("user_query", "")

    prompt = f"""사용자 입력: "{user_query}"

[1단계] 불용어 제거
"찾아줘", "알려줘", "연구한", "논문을", "좀", "관련" 등 탐색 의도 표현 제거.

[2단계] 핵심 명사구 추출
남은 텍스트에서 연구 주제에 해당하는 명사구만 추출.

[3단계] 학술 키워드 변환
추출된 개념을 학술 논문 검색에 쓰이는 용어로 변환. 한글명 + 영문명 + 한 줄 설명.

[4단계] 신뢰도 기준 상위 3개 선정
기준: 원본 입력과의 의미 일치도, 학술 검색어로서의 구체성.
3개 미만이면 찾은 것만 반환.

JSON만 반환:
{{
  "keywords": [
    {{"ko": "키워드1", "en": "Keyword1", "desc": "설명"}},
    {{"ko": "키워드2", "en": "Keyword2", "desc": "설명"}}
  ]
}}"""

    result = await _llm_json(_KEYWORD_SYSTEM, prompt, max_tokens=600)
    candidates = result.get("keywords", [])[:3]

    filters = dict(state.get("filters") or empty_filters())
    filters["keywords"] = [c["ko"] for c in candidates]

    return {**state, "filters": filters}


# ── node_free_input_classifier: 자유입력 전용, 통합 프롬프트(의도분류+확장키워드) ──

async def node_free_input_classifier(state: SearchState) -> SearchState:
    """2턴 이상(자유입력) 전용. 의도분류+확장시 키워드추출을 LLM 1회로 통합."""
    messages = state.get("messages") or []
    user_message = messages[-1]["content"] if messages else ""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""오늘은 {today}입니다.

사용자 입력: "{user_message}"

[의도 분류]
- "좁히기": 기존 결과 범위를 줄이는 조건 요청 (연도/논문유형/인용수 관련)
- "확장": 기존 주제에 인접한 새 키워드를 추가로 찾아달라는 요청
- "무관": 논문 탐색과 관계없는 발화
- "주제변경": 완전히 다른 주제로 바꾸려는 요청

[intent="좁히기"일 때] params.filter에 아래 중 해당하는 필드만 채운다 (언급 안 된 필드는 null):
- pub_year_start: 정수 연도. "최근 3년"처럼 상대 표현이면 오늘 날짜 기준으로 직접 계산해 절대 연도로 반환
- paper_type: "JAKO"(국내 학술지) | "DIKO"(학위논문) | "JAFO"(해외 학술지) | "CFKO"(학술대회) 중 하나, 언급 없으면 null
- citation_min: 정수. "인용 많은"처럼 모호하면 null (숫자 임의 추정 금지)

[intent="확장"일 때] params.keywords에 아래 4단계를 거쳐 키워드를 채운다 (기존 keyword_extractor와 동일 지침):
[1단계] 불용어 제거 — "찾아줘", "알려줘", "연구한", "논문을", "좀", "관련" 등 제거
[2단계] 핵심 명사구 추출
[3단계] 학술 키워드 변환 — 한글명 + 영문명 + 한 줄 설명
[4단계] 신뢰도 기준 상위 3개 선정

JSON만 반환:
{{
  "intent": "좁히기" | "확장" | "무관" | "주제변경",
  "params": {{
    "filter": {{"pub_year_start": null, "paper_type": null, "citation_min": null}},
    "keywords": [{{"ko": "...", "en": "...", "desc": "..."}}]
  }}
}}"""

    result = await _llm_json(_FREE_INPUT_SYSTEM, prompt, max_tokens=600)
    intent = result.get("intent", "무관")
    params = result.get("params") or {}

    filters = dict(state.get("filters") or empty_filters())
    history = list(state.get("history") or [])

    if intent == "좁히기":
        filter_vals = params.get("filter") or {}
        filters = dict(_apply_filter_update(filters, filter_vals))
        history.append(RefinementStep(
            step_type="narrow", applied_filter=filter_vals, added_keyword=None,
            result_count=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
    elif intent == "확장":
        new_keywords = [c["ko"] for c in (params.get("keywords") or [])]
        for kw in new_keywords:
            filters = dict(_apply_keyword_addition(filters, kw))
        history.append(RefinementStep(
            step_type="expand", applied_filter=None,
            added_keyword=", ".join(new_keywords) or None,
            result_count=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))

    return {
        **state,
        "filters": filters,
        "history": history,
        "_free_input_intent": intent,
    }


# ── node_response_builder: 검색 실행 + 요약 + 칩 생성 ─────────────────────────

def _quantile_bin_index(value: float, edges: list[float]) -> int:
    for i, e in enumerate(edges):
        if value < e:
            return i
    return len(edges)


def _entropy_score(counts: list[int]) -> tuple[float, int]:
    """반환: (정규화 엔트로피, 유효 구간 수 k_eff)"""
    total = sum(counts)
    nonzero = [c for c in counts if c > 0]
    k_eff = len(nonzero)
    if k_eff <= 1 or total == 0:
        return 0.0, k_eff
    entropy = -sum((c / total) * math.log2(c / total) for c in nonzero)
    return entropy / math.log2(k_eff), k_eff


def _numeric_quantile_bins(values: list[float], bin_count: int) -> tuple[list[int], list[float]]:
    """등빈도 분위수로 bin_count개 구간 배정. 값이 몰려있으면 중복 경계 제거로 구간 수 자동 축소."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    edges = []
    for i in range(1, bin_count):
        idx = min(int(n * i / bin_count), n - 1)
        edges.append(sorted_vals[idx])
    edges = sorted(set(edges))
    bin_indices = [_quantile_bin_index(v, edges) for v in values]
    return bin_indices, edges


def _build_narrow_chips(
    result_items: list[dict],
    citation_lookup: dict[str, Optional[int]],
) -> List[NarrowChip]:
    bin_count = settings.chip_bin_count
    threshold = settings.chip_evenness_threshold
    candidates: list[tuple[float, NarrowChip]] = []

    years = [it["year"] for it in result_items if it.get("year") is not None]
    if years:
        bin_indices, edges = _numeric_quantile_bins(years, bin_count)
        counts = [0] * (len(edges) + 1)
        for b in bin_indices:
            counts[b] += 1
        score, k_eff = _entropy_score(counts)
        if k_eff > 1 and score >= threshold:
            top_bin_lower = edges[-1] if edges else min(years)
            candidates.append((score, NarrowChip(
                chip_id="narrow_year", chip_type="year", label="",
                value={"pub_year_start": int(top_bin_lower)},
            )))

    citations = [v for v in citation_lookup.values() if v is not None]
    if citations:
        bin_indices, edges = _numeric_quantile_bins(citations, bin_count)
        counts = [0] * (len(edges) + 1)
        for b in bin_indices:
            counts[b] += 1
        score, k_eff = _entropy_score(counts)
        if k_eff > 1 and score >= threshold:
            top_bin_lower = edges[-1] if edges else min(citations)
            candidates.append((score, NarrowChip(
                chip_id="narrow_citation", chip_type="citation", label="",
                value={"citation_min": int(top_bin_lower)},
            )))

    types = [it["db_code"] for it in result_items if it.get("db_code")]
    if types:
        type_counts = Counter(types)
        score, k_eff = _entropy_score(list(type_counts.values()))
        if k_eff > 1 and score >= threshold:
            most_common_type = type_counts.most_common(1)[0][0]
            candidates.append((score, NarrowChip(
                chip_id="narrow_paper_type", chip_type="paper_type", label="",
                value={"paper_type": most_common_type},
            )))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [chip for _, chip in candidates[:3]]


async def _build_expand_chips(keywords: list[str]) -> List[ExpandChip]:
    """매칭 키워드별 find_related_keywords 호출 후 합산(sum) 병합, 상위 3개."""
    from app.core.neo4j_client import get_neo4j_driver
    from app.repositories.graph_repository import GraphRepository

    if not keywords:
        return []

    driver = get_neo4j_driver()
    merged: dict[str, dict] = {}
    try:
        repo = GraphRepository(driver)
        for kw in keywords:
            center = repo.find_keyword(kw)
            if not center:
                continue
            related = repo.find_related_keywords(center["key"], limit=10, min_paper_count=1)
            for item in related:
                node = item["node"]
                key = node["key"]
                count = item["edge"].get("paper_count", 0)
                if key in merged:
                    merged[key]["count"] += count
                else:
                    merged[key] = {"name": node.get("name", key), "count": count}
    finally:
        driver.close()

    top3 = sorted(merged.values(), key=lambda x: x["count"], reverse=True)[:3]
    return [
        ExpandChip(
            chip_id=f"expand_{i}",
            chip_type="expand",
            keyword=item["name"],
            label="",
            co_occurrence_count=item["count"],
        )
        for i, item in enumerate(top3)
    ]


async def _build_summary(result_items: list[dict], user_query: str) -> tuple[Optional[str], bool]:
    if not result_items:
        return None, False
    try:
        titles = "\n".join(f"- {it['title']}" for it in result_items[:10])
        resp = await chat(
            messages=[{"role": "user", "content":
                f"다음은 '{user_query}' 검색 결과 논문 제목 목록이다:\n{titles}\n\n"
                f"이 결과들을 2~3문장으로 요약하라."}],
            model=_MODEL, temperature=0.3, max_tokens=300,
        )
        return resp.text.strip(), False
    except Exception as e:
        logger.warning("검색 결과 요약 실패: %s", e)
        return None, True


async def node_response_builder(state: SearchState) -> SearchState:
    intent = state.get("_free_input_intent")

    if intent == "무관":
        return {**state, "ai_summary": None, "fallback": "off_topic"}
    if intent == "주제변경":
        return {**state, "ai_summary": None, "fallback": "topic_change"}

    filters = state.get("filters") or empty_filters()
    svc = get_chroma_search_service()
    items = await svc.search(
        query=" ".join(filters.get("keywords") or []),
        n_results=20,
        pub_year_start=filters.get("pub_year_start"),
        paper_type=filters.get("paper_type"),
        citation_min=filters.get("citation_min"),
    )
    result_items = [it.model_dump() for it in items]

    # citation_count는 칩 계산 전용 lookup으로만 사용 — result_items에는 병합 안 함
    # (6단계 enrich_items_with_credibility 완성 전까지 반쪽 배지 노출 방지)
    ids = [it["paper_id"] for it in result_items]
    citation_lookup = await svc.get_citation_counts(ids)

    total_count = len(result_items)

    history = list(state.get("history") or [])
    if history:
        history[-1] = {**history[-1], "result_count": total_count}
    else:
        history.append(RefinementStep(
            step_type="search", applied_filter=None, added_keyword=None,
            result_count=total_count, timestamp=datetime.now(timezone.utc).isoformat(),
        ))

    narrow_chips = _build_narrow_chips(result_items, citation_lookup) if total_count > 4 else []
    expand_chips = await _build_expand_chips(filters.get("keywords") or [])
    ai_summary, summary_failed = await _build_summary(result_items, state.get("user_query", ""))

    threshold = settings.search_broad_result_threshold
    is_broad_result = threshold is not None and total_count >= threshold

    return {
        **state,
        "result_items": result_items,
        "total_count": total_count,
        "narrow_chips": narrow_chips,
        "expand_chips": expand_chips,
        "ai_summary": ai_summary,
        "summary_failed": summary_failed,
        "is_broad_result": is_broad_result,
        "history": history,
    }


# ── node_router: 폴백 판정만 ──────────────────────────────────────────────

def node_router(state: SearchState) -> SearchState:
    filters = state.get("filters") or empty_filters()
    total_count = state.get("total_count", 0)

    if not filters.get("keywords"):
        return {**state, "fallback": "clarify"}
    if total_count == 0:
        return {**state, "fallback": "no_result"}

    return {**state, "fallback": state.get("fallback")}


# ── 그래프 조립: 조건부 엔트리포인트 ──────────────────────────────────────────

def _entry_router(state: dict) -> str:
    """_skip_classification(칩 클릭) 최우선 확인 → 세션에 filters.keywords/history가
    이미 있으면 자유입력 경로, 없으면 최초 검색.

    파라미터 타입을 SearchState가 아닌 dict로 둔다 — LangGraph가 조건부 엔트리포인트
    라우팅 함수의 파라미터 타입 힌트를 보고 그 스키마에 선언된 키만 남기고 나머지를
    걸러낸 뒤 호출하는 것으로 확인됨. state: SearchState로 선언하면 SearchState에
    없는 임시 키(_skip_classification)가 라우팅 함수 도달 전에 사라진다."""
    if state.get("_skip_classification"):
        return "response_builder"
    filters = state.get("filters") or {}
    history = state.get("history") or []
    if filters.get("keywords") or history:
        return "free_input_classifier"
    return "intent_extractor"


def build_graph():
    workflow = StateGraph(SearchState)
    workflow.add_node("intent_extractor", node_intent_extractor)
    workflow.add_node("keyword_extractor", node_keyword_extractor)
    workflow.add_node("free_input_classifier", node_free_input_classifier)
    workflow.add_node("response_builder", node_response_builder)
    workflow.add_node("router", node_router)

    workflow.set_conditional_entry_point(
        _entry_router,
        {
            "intent_extractor": "intent_extractor",
            "free_input_classifier": "free_input_classifier",
            "response_builder": "response_builder",
        },
    )
    workflow.add_edge("intent_extractor", "keyword_extractor")
    workflow.add_edge("keyword_extractor", "response_builder")
    workflow.add_edge("free_input_classifier", "response_builder")
    workflow.add_edge("response_builder", "router")
    workflow.add_edge("router", END)

    return workflow.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
