from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict

from langgraph.graph import END, StateGraph

from app.core.settings import settings
from app.langgraph.search_state import (
    SearchState,
    SlotState,
    build_search_preview,
    calc_completeness,
    empty_slots,
)
from app.services.llm.client import chat

logger = logging.getLogger(__name__)

_MODEL = settings.llm_default_model

SLOT_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "research_purpose": {
        "context": "목적에 따라 추천 결과가 달라져요.",
        "question": "찾으시는 논문을 어떤 용도로 활용하실 계획인가요?",
        "options": [
            {"label": "연구 주제 탐색 (아직 방향을 잡는 단계)", "value": "연구주제탐색"},
            {"label": "논문 작성 참고 (선행연구로 인용할 논문)", "value": "논문작성참고"},
            {"label": "랩미팅/발표 준비 (최근 주요 연구 위주)", "value": "랩미팅발표"},
            {"label": "최신 트렌드 파악 (리뷰/메타분석 위주)", "value": "최신트렌드"},
        ],
        "allow_multiple": False,
    },
    "paper_scope": {
        "context": "범위에 따라 검색하는 논문 종류가 달라져요.",
        "question": "어떤 범위의 논문을 탐색할까요?",
        "options": [
            {"label": "KCI (국내 등재 학술지)", "value": "KCI"},
            {"label": "SCI/SSCI/A&HCI (국제 등재 학술지)", "value": "SCI"},
            {"label": "둘 다 포함", "value": "ALL"},
            {"label": "상관없음", "value": "ANY"},
        ],
        "allow_multiple": False,
    },
    "pub_year_range": {
        "context": "최신 연구만 필요하시면 범위를 좁혀드릴게요.",
        "question": "논문의 발행 시기 범위도 설정하시겠어요?",
        "options": [
            {"label": f"최근 3년 ({datetime.now().year - 3}~현재)", "value": "3Y"},
            {"label": f"최근 5년 ({datetime.now().year - 5}~현재)", "value": "5Y"},
            {"label": f"최근 10년 ({datetime.now().year - 10}~현재)", "value": "10Y"},
            {"label": "전체", "value": "YEAR_ALL"},
            {"label": "건너뛰기", "value": "SKIP"},
        ],
        "allow_multiple": False,
    },
}

_INTENT_SYSTEM = """슬롯 추출기. 사용자 입력과 현재 슬롯 상태를 보고 JSON만 반환.
절대 설명하지 말 것. JSON 외 텍스트 금지."""

_KEYWORD_SYSTEM = """학술 키워드 추출기. 사용자 입력에서 연구 키워드를 추출해 JSON만 반환.
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
        # JSON 블록 안에 감싸진 경우 처리
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning("LLM JSON 파싱 실패: %s", e)
        return {}


async def node_intent_extractor(state: SearchState) -> SearchState:
    slots = state.get("slots") or empty_slots()
    messages = state.get("messages") or []
    user_message = messages[-1]["content"] if messages else state.get("user_query", "")

    prompt = f"""현재 슬롯 상태:
{json.dumps(slots, ensure_ascii=False)}

사용자 입력:
"{user_message}"

위 입력에서 아래 슬롯에 해당하는 값을 추출하라.

슬롯 정의:
- research_purpose:
    "연구주제탐색" (방향 탐색 중, 주제를 아직 못 정함)
  | "논문작성참고" (논문 작성 중, 선행연구·인용 논문 필요)
  | "랩미팅발표" (랩미팅·발표 준비, 최근 주요 연구 파악)
  | "최신트렌드" (분야 동향·트렌드 파악, 리뷰·메타분석 위주)
  | "기타"
- paper_scope:
    "KCI" (국내 학술지, KCI 등재, 한국 논문)
  | "SCI" (국제 학술지, SCI·SSCI·AHCI, 해외 논문)
  | "ALL" (둘 다 포함, 국내외 모두, 제한 없음)
  | "ANY" (상관없음, 아무거나)
- pub_year_range:
    "3Y" (최근 3년, {datetime.now().year - 3}년 이후)
  | "5Y" (최근 5년, {datetime.now().year - 5}년 이후)
  | "10Y" (최근 10년, {datetime.now().year - 10}년 이후)
  | "YEAR_ALL" (전체, 연도 제한 없음, 오래된 논문도 포함)
  | "SKIP" (건너뛰기, 모르겠음, 연도 상관없음)
- advanced_filter: 구체화도와 무관한 추가 조건 문자열 (예: "한국 저자만")
- topic_change: 주제 변경 의도인지 (true/false)
- off_topic: 논문 탐색과 무관한 입력인지 (true/false)

이미 채워진 슬롯에 다른 값이 들어오면 conflict_slot에 기록.

JSON만 반환:
{{
  "extracted": {{
    "research_purpose": null,
    "paper_scope": null,
    "pub_year_range": null
  }},
  "advanced_filter": null,
  "topic_change": false,
  "off_topic": false,
  "conflict_slot": null,
  "conflict_new_value": null
}}"""

    result = await _llm_json(_INTENT_SYSTEM, prompt)
    extracted = result.get("extracted", {})
    logger.warning("[intent] user_message=%r extracted=%s slots_before=%s", user_message, extracted, dict(slots))

    new_slots = dict(slots)
    code_conflict_slot: str | None = None
    code_conflict_value: str | None = None
    for key in ("research_purpose", "paper_scope", "pub_year_range"):
        val = extracted.get(key)
        existing = new_slots.get(key)
        if val and existing and existing != val:
            # LLM 응답 무관하게 코드 레벨에서 충돌 기록
            code_conflict_slot = key
            code_conflict_value = val
        elif val and not existing:
            new_slots[key] = val

    adv_filter = result.get("advanced_filter")
    adv_filters = dict(state.get("advanced_filters") or {})
    if adv_filter:
        adv_filters["extra"] = adv_filter

    # 코드 레벨 충돌이 감지됐으면 LLM 결과를 덮어씀
    if code_conflict_slot:
        result = {
            **result,
            "conflict_slot": code_conflict_slot,
            "conflict_new_value": code_conflict_value,
        }

    logger.warning("[intent] slots_after=%s", dict(new_slots))
    return {
        **state,
        "slots": new_slots,
        "advanced_filters": adv_filters,
        "_intent_result": result,
    }


async def node_keyword_extractor(state: SearchState) -> SearchState:
    keyword_mode = state.get("keyword_mode")
    # edit/add 모드는 선택지를 더 많이 제시하기 위해 후보를 최대 5개까지 추출
    max_count = 5 if keyword_mode in ("edit", "add") else 3

    user_query = state.get("user_query", "")
    messages = state.get("messages") or []
    if messages:
        user_query = messages[-1]["content"]

    prompt = f"""사용자 입력: "{user_query}"

[1단계] 불용어 제거
"찾아줘", "알려줘", "연구한", "논문을", "좀", "관련" 등 탐색 의도 표현 제거.

[2단계] 핵심 명사구 추출
남은 텍스트에서 연구 주제에 해당하는 명사구만 추출.

[3단계] 학술 키워드 변환
추출된 개념을 학술 논문 검색에 쓰이는 용어로 변환. 한글명 + 영문명 + 한 줄 설명.

[4단계] 신뢰도 기준 상위 {max_count}개 선정
기준: 원본 입력과의 의미 일치도, 학술 검색어로서의 구체성.
{max_count}개 미만이면 찾은 것만 반환.

JSON만 반환:
{{
  "keywords": [
    {{"ko": "키워드1", "en": "Keyword1", "desc": "설명"}},
    {{"ko": "키워드2", "en": "Keyword2", "desc": "설명"}}
  ],
  "extraction_count": 2
}}"""

    result = await _llm_json(_KEYWORD_SYSTEM, prompt, max_tokens=600)
    candidates = result.get("keywords", [])[:max_count]

    new_slots = dict(state.get("slots") or empty_slots())

    if keyword_mode == "add":
        # add 모드: 이미 선택된 키워드와 중복 제거 — 슬롯은 사용자가 선택 후 확정
        existing = list(new_slots.get("keywords") or [])
        candidates = [c for c in candidates if c["ko"] not in existing]

    elif keyword_mode == "edit":
        # edit 모드: 슬롯 자동 설정 안 함 — 사용자가 후보를 보고 직접 선택
        pass

    else:
        # 일반 모드: 슬롯에 자동 설정 후 1회 확인
        if candidates:
            new_slots["keywords"] = [c["ko"] for c in candidates]

    return {
        **state,
        "slots": new_slots,
        "keyword_candidates": candidates,
    }


def node_response_builder(state: SearchState) -> SearchState:
    slots = state.get("slots") or empty_slots()
    completeness = calc_completeness(slots)
    intent_result = state.get("_intent_result") or {}
    candidates = state.get("keyword_candidates") or []
    keyword_mode = state.get("keyword_mode")

    def _preview():
        return build_search_preview(slots, state.get("user_query", ""))

    # 슬롯 추출 성공 여부 확인 메시지 조각
    confirm_parts = []
    for key, label in [("research_purpose", "연구 목적"), ("paper_scope", "논문 범위"), ("pub_year_range", "발행 연도")]:
        if intent_result.get("extracted", {}).get(key) and slots.get(key):
            confirm_parts.append(f"{label}은 '{slots[key]}'로 설정됐어요.")

    # off_topic
    if intent_result.get("off_topic"):
        msg = "논문 탐색은 언제든 이어서 도와드릴게요. 다시 탐색 조건으로 돌아갈까요?"
        options = [
            {"label": "네, 탐색 계속할게요", "value": "continue"},
            {"label": "처음부터 다시할게요", "value": "restart"},
        ]
        return {**state, "ai_message": msg, "options": options, "allow_multiple": False,
                "search_preview": _preview()}

    # conflict
    if intent_result.get("conflict_slot"):
        slot = intent_result["conflict_slot"]
        old_val = slots.get(slot)
        new_val = intent_result.get("conflict_new_value")
        msg = f"'{old_val}'에서 '{new_val}'(으)로 바꾸시는 거 맞나요?"
        options = [
            {"label": "네, 바꿀게요", "value": f"confirm_change:{slot}:{new_val}"},
            {"label": "둘 다 포함", "value": f"merge:{slot}"},
            {"label": "기존 유지", "value": "keep"},
        ]
        return {**state, "ai_message": msg, "options": options, "allow_multiple": False,
                "search_preview": _preview()}

    # topic_change
    if intent_result.get("topic_change"):
        msg = "혹시 새로운 주제로 바꾸시는 건가요?"
        options = [
            {"label": "현재 주제로 계속 탐색", "value": "keep_topic"},
            {"label": "새 주제로 처음부터 검색", "value": "new_topic"},
        ]
        return {**state, "ai_message": msg, "options": options, "allow_multiple": False,
                "search_preview": _preview()}

    # ── 키워드 edit 모드: 후보 최대 5개를 개별 선택지로 제시 ─────────────
    if keyword_mode == "edit":
        if not candidates:
            msg = "수정할 키워드 후보가 없어요. 검색어를 다시 입력해주세요."
            return {**state, "ai_message": msg, "options": [], "allow_multiple": False,
                    "search_preview": _preview()}
        existing_label = " / ".join(f"#{k}" for k in (slots.get("keywords") or []))
        prefix = f"현재: {existing_label}\n" if existing_label else ""
        opts = [{"label": f"#{c['ko']} — {c['desc']}", "value": f"select_kw:{c['ko']}"}
                for c in candidates]
        msg = f"{prefix}사용할 키워드를 선택해주세요. (복수 선택 가능)"
        return {**state, "ai_message": msg, "options": opts, "allow_multiple": True,
                "search_preview": _preview()}

    # ── 키워드 add 모드: 추가 후보 제시 ─────────────────────────────────
    if keyword_mode == "add":
        existing_label = " / ".join(f"#{k}" for k in (slots.get("keywords") or []))
        if not candidates:
            msg = (f"현재 키워드: {existing_label}\n"
                   "추가할 키워드 후보를 찾지 못했어요. 직접 입력해주세요.")
            return {**state, "ai_message": msg, "options": [], "allow_multiple": False,
                    "search_preview": _preview()}
        opts = [{"label": f"#{c['ko']} — {c['desc']}", "value": f"select_kw:{c['ko']}"}
                for c in candidates]
        msg = f"현재 키워드: {existing_label}\n추가할 키워드를 선택하세요. (복수 선택 가능)"
        return {**state, "ai_message": msg, "options": opts, "allow_multiple": True,
                "search_preview": _preview()}

    # keywords 슬롯은 "묶음 30%": 1개 이상이면 개수 무관하게 슬롯 충족(30%).
    # cnt 2/1 분기의 "추가 권유"는 점수 게이트가 아니라 검색 품질 향상 제안 —
    # 슬롯은 이미 충족됐고, 더 넣으면 정확도가 올라간다는 안내
    # cnt >= 3 / 2 / 1 / 0(모호) 네 경우로 분기하며, 각 return 시
    # keyword_candidates=[] 로 초기화해 다음 턴 재표시를 방지
    if candidates is not None and not slots.get("keywords"):
        cnt = len(candidates)
        kw_labels = " / ".join(f"#{c['ko']}" for c in candidates)

        if cnt >= 3:
            msg = f"입력하신 내용에서 핵심 키워드 {cnt}개를 뽑아봤어요.\n{kw_labels}\n이걸로 검색하면 될까요?"
            opts = [
                {"label": "이대로 검색", "value": "confirm_keywords"},
                {"label": "수정하기", "value": "edit_keywords"},
                {"label": "더 추가하기", "value": "add_keywords"},
            ]
            return {**state, "ai_message": msg, "options": opts, "allow_multiple": True,
                    "keyword_candidates": [], "search_preview": _preview()}

        elif cnt == 2:
            msg = f"핵심 키워드 {cnt}개를 찾았어요. {kw_labels}\n1개 더 추가하시거나, 이대로 진행할까요?"
            opts = [
                {"label": "이대로 진행", "value": "confirm_keywords"},
                {"label": "키워드 추가", "value": "add_keywords"},
            ]
            return {**state, "ai_message": msg, "options": opts, "allow_multiple": False,
                    "keyword_candidates": [], "search_preview": _preview()}

        elif cnt == 1:
            msg = (f"키워드 1개(#{candidates[0]['ko']})를 찾았어요.\n"
                   "추가 키워드를 넣으면 검색 정확도가 올라가요.")
            opts = [
                {"label": "이대로 진행", "value": "confirm_keywords"},
                {"label": "키워드 추가", "value": "add_keywords"},
            ]
            return {**state, "ai_message": msg, "options": opts, "allow_multiple": False,
                    "keyword_candidates": [], "search_preview": _preview()}

        else:  # cnt == 0: 모호 입력
            msg = ("핵심 키워드를 찾지 못했어요. 연구 주제를 조금 더 구체적으로 알려주시겠어요?\n"
                   "예: 특정 분야, 기술, 현상 등을 포함해서 입력해보세요.")
            opts = [
                {"label": "분야를 좁혀볼게요", "value": "narrow_field"},
                {"label": "처음부터 다시 입력", "value": "restart"},
            ]
            return {**state, "ai_message": msg, "options": opts, "allow_multiple": False,
                    "keyword_candidates": [], "search_preview": _preview()}

    # completeness >= 100
    if completeness >= 100:
        # start_search 클릭 후 재진입 시 같은 메시지 중복 방지
        if state.get("search_ready"):
            return state
        msg = "충분한 조건이 모였어요! 논문 탐색을 시작할게요."
        options = [{"label": "논문 탐색 시작하기", "value": "start_search"}]
        return {**state, "ai_message": msg, "options": options, "allow_multiple": False,
                "search_ready": True,
                "search_preview": build_search_preview(slots, state.get("user_query", ""))}

    # 다음 빈 슬롯 질문
    priority = ["research_purpose", "paper_scope", "keywords", "pub_year_range"]
    next_slot = next((s for s in priority if not slots.get(s)), None)
    logger.warning("[response_builder] completeness=%s next_slot=%s slots=%s", completeness, next_slot, dict(slots))

    if next_slot and next_slot in SLOT_QUESTIONS:
        q = SLOT_QUESTIONS[next_slot]
        prefix = " ".join(confirm_parts) + " " if confirm_parts else ""
        msg = f"{prefix}{q['context']}\n{q['question']}"
        return {**state, "ai_message": msg.strip(), "options": q["options"],
                "allow_multiple": q["allow_multiple"],
                "search_preview": build_search_preview(slots, state.get("user_query", ""))}

    # 90%+: 조건이 충분해 강조 권유 (프론트는 search_stage="emphasized"로 버튼 강조)
    if completeness >= 90:
        msg = "조건이 충분해요! 지금 바로 탐색해볼까요?"
        options = [{"label": "논문 탐색 시작하기", "value": "start_search"}]
        return {**state, "ai_message": msg, "options": options, "allow_multiple": False,
                "search_ready": True,
                "search_preview": _preview()}

    # 80–89%: 탐색 가능 (프론트는 search_stage="ready"로 버튼 활성)
    if completeness >= 80:
        msg = "탐색 준비가 됐어요! 논문을 찾아볼까요?"
        options = [{"label": "논문 탐색 시작하기", "value": "start_search"}]
        return {**state, "ai_message": msg, "options": options, "allow_multiple": False,
                "search_ready": True,
                "search_preview": _preview()}

    # 80% 미만 + 강제 시작 요청
    if completeness < 80:
        msg = ("아직 필수 정보가 부족해서 검색 정확도가 낮을 수 있어요. 그래도 진행하시겠어요?\n"
               "('연구 목적'만 알려주시면 정확도가 훨씬 올라가요)")
        options = [
            {"label": "연구 목적 알려주기", "value": "tell_purpose"},
            {"label": "지금 상태로 탐색", "value": "force_start"},
        ]
        return {**state, "ai_message": msg, "options": options, "allow_multiple": False,
                "search_preview": build_search_preview(slots, state.get("user_query", ""))}

    # 기본: 다음 빈 슬롯
    msg = "탐색 조건을 조금 더 구체화해볼게요."
    return {**state, "ai_message": msg, "options": [], "allow_multiple": False,
            "search_preview": build_search_preview(slots, state.get("user_query", ""))}


def node_router(state: SearchState) -> SearchState:
    slots = state.get("slots") or empty_slots()
    force_start = any(
        m.get("content") in ("force_start", "start_search")
        for m in (state.get("messages") or [])
        if m.get("role") == "user"
    )

    completeness = calc_completeness(slots)
    search_ready = state.get("search_ready", False) or force_start or completeness >= 80

    if search_ready and slots.get("keywords"):
        final_params = _build_search_params(state)
        return {**state, "search_ready": True, "final_search_params": final_params}

    return {**state, "search_ready": False}


def _build_search_params(state: SearchState):
    from app.langgraph.search_state import SearchParams
    slots = state.get("slots") or empty_slots()
    _y = datetime.now().year
    year_map = {"3Y": _y - 3, "5Y": _y - 5, "10Y": _y - 10}
    pub_year_start = year_map.get(slots.get("pub_year_range") or "", None)
    return SearchParams(
        keywords=slots.get("keywords") or [],
        scope=slots.get("paper_scope") or "ANY",
        pub_year_start=pub_year_start,
        research_purpose=slots.get("research_purpose") or "연구주제탐색",
        trust_level=None,
        advanced_filters=state.get("advanced_filters") or {},
    )


def _should_extract_keywords(state: SearchState) -> str:
    slots = state.get("slots") or empty_slots()
    intent = state.get("_intent_result") or {}
    keyword_mode = state.get("keyword_mode")

    if intent.get("off_topic") or intent.get("topic_change"):
        return "response_builder"

    # add 모드: 추가 후보를 얻기 위해 항상 재추출
    if keyword_mode == "add":
        return "keyword_extractor"

    # edit 모드: 기존 후보가 없을 때만 재추출, 있으면 그대로 제시
    if keyword_mode == "edit":
        return "keyword_extractor" if not state.get("keyword_candidates") else "response_builder"

    # 일반 모드: 키워드 슬롯이 비어있을 때만 추출
    if not slots.get("keywords"):
        return "keyword_extractor"

    return "response_builder"


def build_graph():
    workflow = StateGraph(SearchState)
    workflow.add_node("intent_extractor", node_intent_extractor)
    workflow.add_node("keyword_extractor", node_keyword_extractor)
    workflow.add_node("response_builder", node_response_builder)
    workflow.add_node("router", node_router)

    workflow.set_entry_point("intent_extractor")
    workflow.add_conditional_edges(
        "intent_extractor",
        _should_extract_keywords,
        {"keyword_extractor": "keyword_extractor", "response_builder": "response_builder"},
    )
    workflow.add_edge("keyword_extractor", "response_builder")
    workflow.add_edge("response_builder", "router")
    workflow.add_edge("router", END)

    return workflow.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
