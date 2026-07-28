from __future__ import annotations

import json
import logging
import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
from app.core.database import AsyncSessionLocal
from app.repositories.paper_repository import get_paper_cards_batch
from app.schemas.search import CredibilityInfo
from app.services.bookmark_service import get_bookmarked_paper_ids
from app.services.chroma_search_service import get_chroma_search_service
from app.services.credibility_service import enrich_items_with_credibility, paper_type_label, resolve_paper_type
from app.services.llm.client import chat
from app.services.search_service import _apply_sort_order, _SORT_LABELS, _sort_items

logger = logging.getLogger(__name__)

_MODEL = settings.llm_default_model
_kiwi = Kiwi()  # 모듈 로드 시 1회 초기화 (싱글턴)

_KEYWORD_SYSTEM = """학술 키워드·필터 추출기. 사용자 입력에서 연구 키워드와 필터 조건(연도/논문유형/인용수)을 추출해 JSON만 반환. 절대 설명하지 말 것. JSON 외 텍스트 금지."""

_FREE_INPUT_SYSTEM = """검색 정교화 의도 분류기. 사용자 입력을 보고 JSON만 반환.
절대 설명하지 말 것. JSON 외 텍스트 금지."""


async def _llm_json(system: str, user: str, max_tokens: int = 1000) -> dict:
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


# ── node_keyword_extractor: 최초 검색 경로 전용 ──────────────────────────────

async def node_keyword_extractor(state: SearchState) -> SearchState:
    """최초 검색 경로 전용. 키워드 추출(불용어 제거→명사구 추출→학술 키워드 변환→상위 3개)
    프롬프트는 기존 그대로 두고, 첫 질의에 함께 언급된 필터 조건(연도/논문유형/인용수)도
    같은 LLM 호출로 추출해 반영한다 — 기존엔 이 경로에서 필터가 통째로 무시됐음."""
    user_query = state.get("user_query", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""오늘은 {today}입니다.

사용자 입력: "{user_query}"

[1단계] 불용어 제거
"찾아줘", "알려줘", "연구한", "논문을", "좀", "관련" 등 탐색 의도 표현 제거.

[2단계] 핵심 명사구 추출
남은 텍스트에서 연구 주제에 해당하는 명사구만 추출.

[3단계] 학술 키워드 변환
추출된 개념을 학술 논문 검색에 쓰이는 용어로 변환. 한글명 + 영문명 + 한 줄 설명.

[4단계] 신뢰도 기준 상위 3개 선정
기준: 원본 입력과의 의미 일치도, 학술 검색어로서의 구체성.
3개 미만이면 찾은 것만 반환.

[필터 조건 추출]
입력에 아래 조건이 명시적으로 언급된 경우에만 채운다 (언급 안 된 필드는 null, 임의 추정 금지):
- pub_year_start: 정수 연도. "최근 3년"처럼 상대 표현이면 오늘 날짜 기준으로 직접 계산해 절대 연도로 반환
- paper_type: "학술 저널"(국내외 학술지·학술대회 포함) | "박사학위 논문" | "석사학위 논문" 중 하나, 언급 없으면 null
- citation_min: 정수. "인용 많은"처럼 모호하면 null (숫자 임의 추정 금지)
- kci_only: "KCI 등재 논문만" 같은 언급이 있으면 true, 언급 없으면 null
- sci_only: "SCI 논문만", "SCI급" 같은 언급이 있으면 true, 언급 없으면 null

JSON만 반환:
{{
  "keywords": [
    {{"ko": "키워드1", "en": "Keyword1", "desc": "설명"}},
    {{"ko": "키워드2", "en": "Keyword2", "desc": "설명"}}
  ],
  "filter": {{"pub_year_start": null, "paper_type": null, "citation_min": null, "kci_only": null, "sci_only": null}}
}}"""

    result = await _llm_json(_KEYWORD_SYSTEM, prompt)
    candidates = result.get("keywords", [])[:3]
    filter_vals = result.get("filter") or {}

    filters = dict(state.get("filters") or empty_filters())
    filters["keywords"] = [c["ko"] for c in candidates]
    filters = dict(_apply_filter_update(filters, filter_vals))

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
- paper_type: "학술 저널"(국내외 학술지·학술대회 포함) | "박사학위 논문" | "석사학위 논문" 중 하나, 언급 없으면 null
- citation_min: 정수. "인용 많은"처럼 모호하면 null (숫자 임의 추정 금지)
- kci_only: "KCI 등재 논문만" 같은 언급이 있으면 true, 언급 없으면 null
- sci_only: "SCI 논문만", "SCI급" 같은 언급이 있으면 true, 언급 없으면 null

[intent="확장"일 때] params.keywords에 아래 4단계를 거쳐 키워드를 채운다 (기존 keyword_extractor와 동일 지침):
[1단계] 불용어 제거 — "찾아줘", "알려줘", "연구한", "논문을", "좀", "관련" 등 제거
[2단계] 핵심 명사구 추출
[3단계] 학술 키워드 변환 — 한글명 + 영문명 + 한 줄 설명
[4단계] 신뢰도 기준 상위 3개 선정

[intent="주제변경"일 때] 기존 조건은 전부 버려지고 이 입력만으로 검색이 새로 시작된다.
params.keywords에 "확장"과 동일한 4단계를 거쳐 새 주제 기준 키워드를 채운다.
같은 입력에 필터 조건(연도/논문유형/인용수/KCI/SCI)이 함께 언급됐으면 params.filter에도 채운다 (언급 없으면 null).

JSON만 반환:
{{
  "intent": "좁히기" | "확장" | "무관" | "주제변경",
  "params": {{
    "filter": {{"pub_year_start": null, "paper_type": null, "citation_min": null, "kci_only": null, "sci_only": null}},
    "keywords": [{{"ko": "...", "en": "...", "desc": "..."}}]
  }}
}}"""

    result = await _llm_json(_FREE_INPUT_SYSTEM, prompt)
    intent = result.get("intent", "무관")
    params = result.get("params") or {}

    filters = dict(state.get("filters") or empty_filters())
    history = list(state.get("history") or [])

    if intent == "좁히기":
        filter_vals = params.get("filter") or {}
        filters = dict(_apply_filter_update(filters, filter_vals))
        history.append(RefinementStep(
            step_id=str(uuid.uuid4()),
            step_type="narrow", applied_filter=filter_vals, added_keyword=None,
            result_count=0, result_items=[],
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
    elif intent == "확장":
        new_keywords = [c["ko"] for c in (params.get("keywords") or [])]
        for kw in new_keywords:
            filters = dict(_apply_keyword_addition(filters, kw))
        history.append(RefinementStep(
            step_id=str(uuid.uuid4()),
            step_type="expand", applied_filter=None,
            added_keyword=", ".join(new_keywords) or None,
            result_count=0, result_items=[],
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
    elif intent == "주제변경":
        # 검색 조건(filters)은 새 주제 기준으로 완전히 리셋 — 이전 주제 키워드와 섞이면 안 됨.
        # history(탐색 로그)는 비우지 않고 이어서 추가 — 이전 검색 결과가 채팅에 계속 누적돼 보여야 함.
        new_keywords = [c["ko"] for c in (params.get("keywords") or [])]
        filter_vals = params.get("filter") or {}
        filters = dict(_apply_filter_update(empty_filters(), filter_vals))
        filters["keywords"] = new_keywords
        history.append(RefinementStep(
            step_id=str(uuid.uuid4()),
            step_type="search", applied_filter=None, added_keyword=None,
            result_count=0, result_items=[],
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))

    new_state = {
        **state,
        "filters": filters,
        "history": history,
        "_free_input_intent": intent,
    }
    if intent == "주제변경":
        new_state["user_query"] = user_message
    return new_state


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
            year_value = int(top_bin_lower)
            candidates.append((score, NarrowChip(
                chip_id="narrow_year", chip_type="year", label=f"{year_value}년 이후 논문만 보기",
                value={"pub_year_start": year_value},
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
            citation_value = int(top_bin_lower)
            candidates.append((score, NarrowChip(
                chip_id="narrow_citation", chip_type="citation", label=f"인용수 {citation_value}회 이상만 보기",
                value={"citation_min": citation_value},
            )))

    types = [it["paper_type"] for it in result_items if it.get("paper_type")]
    if types:
        type_counts = Counter(types)
        score, k_eff = _entropy_score(list(type_counts.values()))
        if k_eff > 1 and score >= threshold:
            most_common_type = type_counts.most_common(1)[0][0]
            candidates.append((score, NarrowChip(
                chip_id="narrow_paper_type", chip_type="paper_type", label=f"{most_common_type}만 보기",
                value={"paper_type": most_common_type},
            )))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [chip for _, chip in candidates[:3]]


def _top_result_keywords(
    result_items: list[dict],
    top_n_papers: int = 10,
    top_k_keywords: int = 5,
) -> list[str]:
    """확장 칩 조회용 키워드 소스 — filters.keywords(사용자 검색어, LLM 파라프레이즈)가 아니라
    실제 검색 결과 상위 논문들의 원본 keywords 필드에서 뽑는다. Neo4j Keyword 노드는
    논문 메타데이터 원본 키워드로 구성돼 있어 사용자 검색어와 어휘가 달라 매칭이 거의 안 됨
    (실측 확인됨) — 결과 논문의 실제 키워드를 쓰면 매칭된다.
    빈도 동점 시 더 상위(관련도 높은) 논문에 먼저 등장한 키워드 우선 (결정론적)."""
    subset = result_items[:top_n_papers]
    freq: Dict[str, int] = {}
    first_index: Dict[str, int] = {}
    for idx, item in enumerate(subset):
        for kw in item.get("keywords") or []:
            freq[kw] = freq.get(kw, 0) + 1
            if kw not in first_index:
                first_index[kw] = idx
    ranked = sorted(freq.keys(), key=lambda kw: (-freq[kw], first_index[kw]))
    return ranked[:top_k_keywords]


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
            label=f"'{item['name']}' 키워드로 확장하기",
            co_occurrence_count=item["count"],
        )
        for i, item in enumerate(top3)
    ]


_SUMMARY_SYSTEM_PROMPT = """너는 논문 검색 결과를 사용자에게 소개하는 문장을 쓰는 도우미다.

반드시 지켜야 할 규칙:
1. 1~2문장으로 간결하게 쓴다.
2. 존댓말을 쓰되 딱딱하지 않게, "~해 드릴게요", "~하시면 좋을 것 같아요" 같은 부드러운 종결어미를 쓴다.
3. 차분하고 사려깊은 톤을 유지한다.
4. 아래로 전달되는 "실제 데이터"의 총 건수와 키워드는 문장에 반드시 포함한다.
5. 전달받지 않은 논문 제목, 저자, 통계, 키워드, 유형 등은 절대로 새로 만들어내지 않는다. 오직 전달받은 사실만 사용한다.
6. 매번 표현을 다르게 써서 같은 문장이 반복되지 않게 한다.
7. 결과 목록을 나열하지 말고, 검색을 어떻게 진행했는지에 대한 요약만 전달한다.
8. 문장 외의 다른 텍스트(따옴표, 설명, 마크다운 등)는 출력하지 않는다."""


async def _build_summary(
    topic: str,
    total_count: int,
    keywords: list[str],
    type_distribution: dict[str, int],
    sort_order: str,
) -> tuple[Optional[str], bool]:
    """검색 결과 요약 문장 생성. 건수/키워드/유형분포는 코드에서 계산한 사실이며,
    LLM은 이 사실을 문장으로 풀어쓰는 역할만 한다 (숫자를 직접 세지 않음)."""
    if total_count == 0:
        return None, False

    sort_label = _SORT_LABELS.get(sort_order, "관련도 기준")
    topic = topic or " ".join(keywords) or "요청하신 주제"

    fact_lines = [
        f"주제: {topic}",
        f"총 건수: {total_count}건",
        f"키워드: {', '.join(keywords) if keywords else '없음'}",
        f"정렬 기준: {sort_label}",
    ]
    if type_distribution:
        dist_str = ", ".join(f"{k} {v}건" for k, v in type_distribution.items())
        fact_lines.append(f"유형 분포: {dist_str}")

    user_prompt = (
        "실제 데이터:\n" + "\n".join(fact_lines) +
        "\n\n위 데이터만 사용해 자연스러운 한국어 문장 1~2개를 작성하라."
    )

    try:
        resp = await chat(
            messages=[
                {"role": "user", "content": f"[System]\n{_SUMMARY_SYSTEM_PROMPT}\n\n[User]\n{user_prompt}"},
            ],
            model=_MODEL, temperature=0.9, use_cache=False,
        )
        text = resp.text.strip()
        return (text, False) if text else (None, True)
    except Exception as e:
        logger.warning("검색 결과 요약 실패: %s", e)
        return None, True


async def node_response_builder(state: SearchState) -> SearchState:
    intent = state.get("_free_input_intent")

    if intent == "무관":
        return {**state, "ai_summary": None, "fallback": "off_topic"}

    filters = state.get("filters") or empty_filters()
    svc = get_chroma_search_service()
    # paper_type은 세션에 "학술 저널"/"박사학위 논문"/"석사학위 논문" 레이블로 저장되지만,
    # Chroma의 DBCode 메타데이터는 원본 코드(JAKO/DIKO/JAFO/CFKO)라 여기선 매칭 안 됨.
    # 박사/석사 구분은 degree 정보가 있어야 하는데 그건 PostgreSQL enrichment 이후에나 알 수 있으므로,
    # 여기서는 DIKO(학위논문)로만 1차 축소하고, 실제 박사/석사/학술 저널 구분은
    # 아래 enrichment 이후 it.paper_type 기준 2차 필터로 처리한다.
    paper_type_filter = filters.get("paper_type")
    chroma_paper_type = "DIKO" if paper_type_filter in ("박사학위 논문", "석사학위 논문") else None
    # similarity_score/snippet은 사전계산 캐시(scripts/embed_abstract_sentences.py)를 쓰므로
    # 후보 수와 무관하게 빠름 — n_results 생략해 where절 필터를 통과한 전체 후보를 받는다.
    # kci_only/sci_only/paper_type은 뒤에서 전체 후보에 대해 적용된다.
    items = await svc.search(
        query=" ".join(filters.get("keywords") or []),
        pub_year_start=filters.get("pub_year_start"),
        paper_type=chroma_paper_type,
        citation_min=filters.get("citation_min"),
        pub_year_exact=bool(filters.get("pub_year_exact")),
    )

    # 칩 엔트로피 계산 전용 lookup
    # (PostgreSQL citation_count는 미매칭 건도 0으로 채워져 있어 엔트로피 계산에 부적합 — 별도 이슈로만 기록)
    ids = [it.paper_id for it in items]
    citation_lookup = await svc.get_citation_counts(ids)

    # 카드에 노출할 배지는 PostgreSQL enrichment로 별도 채움 (citation_count/kci/sci/SJR 등)
    async with AsyncSessionLocal() as db:
        try:
            db_extra = await get_paper_cards_batch(db, ids)
            for it in items:
                extra = db_extra.get(it.paper_id) or {}
                it.credibility = CredibilityInfo(badge="unknown", citation_count=extra.get("citation_count"))
                it.paper_type = paper_type_label(resolve_paper_type(it.db_code, extra.get("degree")))
            items = await enrich_items_with_credibility(items, db)
        except Exception as e:
            logger.warning("검색 결과 신뢰도 enrichment 실패, 배지 없이 진행: %s", e)

        user_id_raw = state.get("user_id")
        if user_id_raw:
            try:
                bookmarked_ids = await get_bookmarked_paper_ids(db, uuid.UUID(user_id_raw), ids)
                for it in items:
                    it.is_bookmarked = it.paper_id in bookmarked_ids
            except Exception as e:
                logger.warning("검색 결과 북마크 여부 조회 실패, false로 진행: %s", e)

    if filters.get("kci_only"):
        items = [it for it in items if it.credibility.kci_registered]
    if filters.get("sci_only"):
        items = [it for it in items if it.credibility.sci_indexed]
    if paper_type_filter:
        items = [it for it in items if it.paper_type == paper_type_filter]

    # items = _apply_scoring(items, research_purpose_class=state.get("research_purpose_class") or "neutral")  # 관련도순 정렬 원칙에 따라 비활성화
    items = _sort_items(items)
    items = _apply_sort_order(items, state.get("sort_order") or "relevance")

    result_items = [it.model_dump() for it in items]
    total_count = len(result_items)

    history = list(state.get("history") or [])
    if history:
        history[-1] = {**history[-1], "result_count": total_count, "result_items": result_items}
    else:
        history.append(RefinementStep(
            step_id=str(uuid.uuid4()),
            step_type="search", applied_filter=None, added_keyword=None,
            result_count=total_count, result_items=result_items,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))

    narrow_chips = _build_narrow_chips(result_items, citation_lookup) if total_count > 4 else []
    expand_chips = await _build_expand_chips(_top_result_keywords(result_items))
    type_distribution = dict(Counter(
        it["paper_type"] for it in result_items if it.get("paper_type")
    ))
    # _skip_classification은 자유입력 분류 노드(LLM)만 건너뛰기 위한 신호일 뿐 — 칩 클릭/필터
    # 직접 지정이어도 검색 결과(total_count/keywords/type_distribution)는 매 턴 새로 계산되므로
    # 요약 문장도 항상 최신 값 기준으로 다시 생성해야 한다. (과거엔 여기서 이전 턴 ai_summary를
    # 그대로 재사용해 "문장은 그대로, 건수는 새 값"으로 어긋나는 버그가 있었음)
    ai_summary, summary_failed = await _build_summary(
        topic=state.get("user_query", ""),
        total_count=total_count,
        keywords=filters.get("keywords") or [],
        type_distribution=type_distribution,
        sort_order=state.get("sort_order") or "relevance",
    )

    threshold = settings.search_broad_result_threshold
    is_broad_result = threshold is not None and total_count >= threshold

    return {
        **state,
        "result_items": result_items,
        "total_count": total_count,
        "type_distribution": type_distribution,
        "narrow_chips": narrow_chips,
        "expand_chips": expand_chips,
        "ai_summary": ai_summary,
        "summary_failed": summary_failed,
        "is_broad_result": is_broad_result,
        "history": history,
        # 이전 턴의 fallback(topic_change/no_result 등)이 **state 스프레드로 새어나오지 않도록 매 정상 턴마다 명시적으로 갱신.
        # topic_change는 여기서도 계속 내려줌 — "감지했다"가 아니라 "감지해서 새 주제로 이미 재검색했다"는 의미로 재해석됨.
        "fallback": "topic_change" if intent == "주제변경" else None,
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
    걸러낸 뒤 호출하는 것으로 확인됨. SearchState에 선언 안 된 임시 키를 라우팅에서만
    쓰고 싶을 때는(스키마에 추가하고 싶지 않을 때) 여기처럼 dict로 받아야 한다."""
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
