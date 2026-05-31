from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.response import success_response
from app.langgraph.search_graph import get_graph
from app.langgraph.search_state import (
    SearchParams,
    SearchPreview,
    SearchState,
    build_search_preview,
    calc_completeness,
    empty_slots,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.search import SearchPapersRequest, SearchPapersResponse
from app.services.search_service import execute_search, search_papers_service

router = APIRouter()

_REDIS_DB = 7
_STATE_TTL = 3600

def _completeness_stage(pct: int) -> str:
    """completeness % → 프론트 UI 분기용 단계 문자열.
    none(<80) / ready(80–89) / emphasized(90–99) / complete(100)"""
    if pct >= 100:
        return "complete"
    if pct >= 90:
        return "emphasized"
    if pct >= 80:
        return "ready"
    return "none"


# confirm_change 화이트리스트 — GAP-3에서 확정된 value 네임스페이스와 일치해야 함
_VALID_SLOT_VALUES: dict[str, frozenset] = {
    "research_purpose": frozenset({"연구주제탐색", "논문작성참고", "랩미팅발표", "최신트렌드", "기타"}),
    "paper_scope":      frozenset({"KCI", "SCI", "ALL", "ANY"}),
    "pub_year_range":   frozenset({"3Y", "5Y", "10Y", "YEAR_ALL", "SKIP"}),
}


# ── 기존 엔드포인트 (유지) ──────────────────────────────────────────────

@router.post(
    "/search/papers",
    response_model=ApiResponse[SearchPapersResponse],
    responses={422: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
    summary="논문 검색 (기존)",
)
async def search_papers(
    request: SearchPapersRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await search_papers_service(request, db)
    return success_response(
        data=result,
        message="paper search completed",
        meta={"page": request.page, "size": request.size, "count": len(result.items)},
    )


# ── PART A 슬롯 대화 ───────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    selected_options: Optional[List[str]] = None
    force_start: bool = False


class ChatResponse(BaseModel):
    session_id: str
    ai_message: str
    options: List[Dict[str, Any]]
    allow_multiple: bool
    search_preview: SearchPreview
    search_ready: bool
    completeness_pct: int
    search_stage: str  # "none" | "ready" | "emphasized" | "complete" — 프론트 UI 분기용
    final_search_params: Optional[SearchParams]


def _load_state(session_id: str) -> Optional[SearchState]:
    r = get_redis(_REDIS_DB)
    raw = r.get(f"search_state:{session_id}")
    if not raw:
        return None
    return json.loads(raw)


def _save_state(session_id: str, state: SearchState) -> None:
    r = get_redis(_REDIS_DB)
    r.set(f"search_state:{session_id}", json.dumps(state, ensure_ascii=False), ex=_STATE_TTL)


def _new_state(session_id: str, user_query: str) -> SearchState:
    slots = empty_slots()
    slots["initial_query"] = True
    preview = build_search_preview(slots, user_query)
    return SearchState(
        user_query=user_query,
        session_id=session_id,
        user_id="",
        slots=slots,
        keyword_candidates=None,
        advanced_filters={},
        messages=[],
        search_preview=preview,
        final_search_params=None,
        search_ready=False,
        keyword_mode=None,
        ai_message="",
        options=[],
        allow_multiple=False,
    )


def _sse(event_type: str, payload: dict) -> str:
    """SSE 한 프레임 직렬화."""
    return f"data: {json.dumps({'type': event_type, **payload}, ensure_ascii=False)}\n\n"


def _apply_selected_options(
    session_id: str,
    state: SearchState,
    opts: list[str] | None,
) -> SearchState:
    """selected_options를 처리해 state에 반영. restart 시 새 state를 반환."""
    if not opts:
        return state

    slots = state.get("slots") or empty_slots()

    # ── 파괴적 옵션 우선 처리 ────────────────────────────────────────────
    if "restart" in opts:
        return _new_state(session_id, "")

    if "new_topic" in opts:
        # initial_query=True 유지: initial_query는 "이 세션에서 대화가
        # 시작됐는가"를 나타내므로 주제 변경과 무관하게 항상 True여야
        # 한다. 새 주제여도 세션 진입 자체는 유지되므로 10%를 보존.
        slots = empty_slots()
        slots["initial_query"] = True
        state["messages"] = []
        state["user_query"] = ""
        state["keyword_candidates"] = None
        state["keyword_mode"] = None
        state["slots"] = slots
        return state

    # ── 일반 옵션 루프 ───────────────────────────────────────────────────
    _selected_kws: list[str] = []

    for opt_value in opts:
        # 시나리오 A: 슬롯 직접 매핑
        if opt_value in ("연구주제탐색", "논문작성참고", "랩미팅발표", "최신트렌드", "기타"):
            slots["research_purpose"] = opt_value
        elif opt_value in ("KCI", "SCI", "ALL", "ANY"):
            slots["paper_scope"] = opt_value
        elif opt_value in ("3Y", "5Y", "10Y", "YEAR_ALL", "SKIP"):
            slots["pub_year_range"] = opt_value

        # 시나리오 A: 슬롯 값 변경 확정
        elif opt_value.startswith("confirm_change:"):
            _, slot, new_val = opt_value.split(":", 2)
            valid_vals = _VALID_SLOT_VALUES.get(slot)
            if valid_vals and new_val in valid_vals:
                slots[slot] = new_val
            else:
                logger.warning(
                    "confirm_change 화이트리스트 불일치 — slot=%r val=%r (무시)", slot, new_val
                )

        # 시나리오 A: 슬롯 병합 — paper_scope만 지원
        elif opt_value.startswith("merge:"):
            target_slot = opt_value.split(":", 1)[1]
            if target_slot == "paper_scope":
                slots["paper_scope"] = "ALL"
            # 다른 슬롯 merge는 미구현

        # GAP-8: 개별 키워드 선택
        elif opt_value.startswith("select_kw:"):
            _selected_kws.append(opt_value[len("select_kw:"):])

        # 키워드 확정
        elif opt_value == "confirm_keywords":
            state["keyword_mode"] = None
            state["keyword_candidates"] = None

        # GAP-8: 키워드 수정/추가 모드
        elif opt_value == "edit_keywords":
            state["keyword_mode"] = "edit"
        elif opt_value == "add_keywords":
            state["keyword_mode"] = "add"

        # 탐색 시작
        elif opt_value == "start_search":
            state["search_ready"] = True

        # 시나리오 C: 슬롯 유지
        elif opt_value in ("keep_topic", "continue"):
            pass

        # 낮은 completeness → 그래프가 research_purpose 유도
        elif opt_value == "tell_purpose":
            pass

    # select_kw 후처리: edit=교체, add=추가
    if _selected_kws:
        mode = state.get("keyword_mode")
        if mode == "add":
            existing = list(slots.get("keywords") or [])
            for kw in _selected_kws:
                if kw not in existing:
                    existing.append(kw)
            slots["keywords"] = existing
        else:
            slots["keywords"] = _selected_kws
        state["keyword_mode"] = None
        state["keyword_candidates"] = None

    state["slots"] = slots
    return state


@router.post("/search/chat", response_model=ApiResponse[ChatResponse])
async def chat_search(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    state = _load_state(session_id) or _new_state(session_id, request.message)

    state["messages"].append({"role": "user", "content": request.message})
    state["user_query"] = state["user_query"] or request.message
    if request.force_start:
        state["messages"].append({"role": "user", "content": "force_start"})

    state = _apply_selected_options(session_id, state, request.selected_options)

    graph = get_graph()
    result_state: SearchState = await graph.ainvoke(state)
    _save_state(session_id, result_state)

    slots = result_state.get("slots") or empty_slots()
    preview = result_state.get("search_preview") or build_search_preview(slots, state["user_query"])
    pct = calc_completeness(slots)

    return success_response(
        data=ChatResponse(
            session_id=session_id,
            ai_message=result_state.get("ai_message", ""),
            options=result_state.get("options") or [],
            allow_multiple=result_state.get("allow_multiple", False),
            search_preview=preview,
            search_ready=result_state.get("search_ready", False),
            completeness_pct=pct,
            search_stage=_completeness_stage(pct),
            final_search_params=result_state.get("final_search_params"),
        ),
        message="chat processed",
    )


@router.post("/search/chat/stream")
async def stream_chat(request: ChatRequest):
    """슬롯 대화 턴을 SSE로 스트리밍.

    ⚠️  클라이언트 수신: fetch + ReadableStream 필수.
        이 엔드포인트는 POST이므로 EventSource(GET 전용)를 사용할 수 없다.
        상세 규격: docs/search-sse-api.md

    이벤트 순서:
      1. slot_update   — selected_options 처리 후 변경된 슬롯 (슬롯별 1회)
      2. completeness  — options 처리 직후 구체화도
      3. keyword_progress {stage:"started"} — 키워드 추출이 예상될 때
      4. (graph.ainvoke 실행 — 블로킹 구간)
      5. slot_update   — 그래프 실행 후 새로 채워진 슬롯
      6. completeness  — 업데이트된 구체화도
      7. keyword_progress {stage:"completed"} — 추출 후 결과
      8. token         — ai_message 서버 chunking (A방식, LLM 스트리밍 아님)
      9. done          — 최종 상태 전체
    error — 예외 발생 시. done 없이 스트림 즉시 종료.
    """
    session_id = request.session_id or str(uuid.uuid4())

    async def generate():
        try:
            state = _load_state(session_id) or _new_state(session_id, request.message)
            state["messages"].append({"role": "user", "content": request.message})
            state["user_query"] = state["user_query"] or request.message
            if request.force_start:
                state["messages"].append({"role": "user", "content": "force_start"})

            # ── 1. selected_options 처리 후 슬롯 diff emit ────────────────
            slots_before = dict(state.get("slots") or empty_slots())
            state = _apply_selected_options(session_id, state, request.selected_options)
            slots_after_opts = state.get("slots") or empty_slots()

            for slot_key in ("research_purpose", "paper_scope", "pub_year_range", "keywords"):
                before_val = slots_before.get(slot_key)
                after_val = slots_after_opts.get(slot_key)
                if after_val != before_val:
                    yield _sse("slot_update", {"slot": slot_key, "value": after_val})

            # ── 2. completeness (options 처리 직후) ───────────────────────
            pct = calc_completeness(slots_after_opts)
            yield _sse("completeness", {"pct": pct, "stage": _completeness_stage(pct)})

            # ── 3. keyword_progress: 추출 예상 시 started emit ───────────
            will_extract = (
                not slots_after_opts.get("keywords")
                or state.get("keyword_mode") in ("edit", "add")
            )
            if will_extract:
                yield _sse("keyword_progress", {"stage": "started"})

            # ── 4. graph.ainvoke (내부 LLM 호출 포함, async 블로킹) ───────
            graph = get_graph()
            result_state: SearchState = await graph.ainvoke(state)
            _save_state(session_id, result_state)

            slots_final = result_state.get("slots") or empty_slots()

            # ── 5. 그래프 실행 후 변경된 슬롯 emit ───────────────────────
            for slot_key in ("research_purpose", "paper_scope", "pub_year_range", "keywords"):
                before_val = slots_after_opts.get(slot_key)
                after_val = slots_final.get(slot_key)
                if after_val != before_val:
                    yield _sse("slot_update", {"slot": slot_key, "value": after_val})

            # ── 6. completeness (그래프 실행 후) ──────────────────────────
            pct = calc_completeness(slots_final)
            yield _sse("completeness", {"pct": pct, "stage": _completeness_stage(pct)})

            # ── 7. keyword_progress: completed ───────────────────────────
            # 키워드 추출은 graph 내부에서 한 번에 완료되므로
            # started/completed 두 단계만 의미 있음 (중간 진행률 없음)
            if will_extract:
                yield _sse("keyword_progress", {
                    "stage": "completed",
                    "keywords": slots_final.get("keywords") or [],
                })

            # ── 8. ai_message chunking → token 이벤트 (A방식: 서버 chunking)
            ai_message = result_state.get("ai_message", "")
            chunk_size = 10
            for i in range(0, len(ai_message), chunk_size):
                yield _sse("token", {"text": ai_message[i:i + chunk_size]})
                await asyncio.sleep(0.015)

            # ── 9. done ───────────────────────────────────────────────────
            preview = (result_state.get("search_preview")
                       or build_search_preview(slots_final, state.get("user_query", "")))
            yield _sse("done", {
                "session_id": session_id,
                "ai_message": ai_message,
                "options": result_state.get("options") or [],
                "allow_multiple": result_state.get("allow_multiple", False),
                "search_ready": result_state.get("search_ready", False),
                "completeness_pct": pct,
                "search_stage": _completeness_stage(pct),
                "search_preview": preview,
                "final_search_params": result_state.get("final_search_params"),
            })

        except Exception:
            logger.exception("SSE 스트리밍 오류 session_id=%s", session_id)
            yield _sse("error", {"message": "서버 오류가 발생했어요. 잠시 후 다시 시도해주세요."})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 응답 버퍼링 비활성화
        },
    )


@router.get("/search/stream/{session_id}")
async def stream_preview(session_id: str):
    """현재 search_preview를 SSE로 반환 (LLM 추가 호출 없음)"""
    async def generate():
        state = _load_state(session_id)
        if state:
            preview = state.get("search_preview") or {}
            yield f"data: {json.dumps({'type': 'preview_update', 'content': preview}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── PART A 실행 검색 ───────────────────────────────────────────────────

class PaperResult(BaseModel):
    paper_id: str
    title: str
    authors: List[str]
    pub_year: Optional[int]
    journal: Optional[str]
    paper_type: Optional[str]
    abstract: Optional[str]
    keywords: List[str]
    scope_badge: Optional[str]
    citation_count: Optional[int]
    relevance_score: float
    trust_badge: None = None
    keyword_map_data: None = None


class ExecuteRequest(BaseModel):
    session_id: str
    search_params: SearchParams


class ExecuteResponse(BaseModel):
    papers: List[PaperResult]
    total: int
    search_id: str


@router.post("/search/execute", response_model=ApiResponse[ExecuteResponse])
async def execute_search_endpoint(
    request: ExecuteRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await execute_search(dict(request.search_params), db)
    return success_response(
        data=ExecuteResponse(
            papers=[PaperResult(**p) for p in result["papers"]],
            total=result["total"],
            search_id=result["search_id"],
        ),
        message="search executed",
    )
