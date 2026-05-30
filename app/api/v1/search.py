from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

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


@router.post("/search/chat", response_model=ApiResponse[ChatResponse])
async def chat_search(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    state = _load_state(session_id) or _new_state(session_id, request.message)

    # 사용자 메시지 추가
    state["messages"].append({"role": "user", "content": request.message})
    state["user_query"] = state["user_query"] or request.message

    # force_start 처리
    if request.force_start:
        state["messages"].append({"role": "user", "content": "force_start"})

    # selected_options 처리 (선택지 클릭 시 슬롯 직접 매핑)
    if request.selected_options:
        opts = request.selected_options
        slots = state.get("slots") or empty_slots()

        # ── 파괴적 옵션 우선 처리 ────────────────────────────────────────
        # restart / new_topic은 루프 전에 단독으로 처리한다.
        # 다른 옵션과 섞여 오더라도 순서에 따라 결과가 달라지는 것을 방지.
        if "restart" in opts:
            # off_topic 복귀: 세션 전체 리셋
            state = _new_state(session_id, "")
            slots = state.get("slots") or empty_slots()

        elif "new_topic" in opts:
            # 시나리오 C: 새 주제로 전환
            # initial_query=True 유지: initial_query는 "이 세션에서 대화가
            # 시작됐는가"를 나타내므로 주제 변경과 무관하게 항상 True여야
            # 한다. 새 주제여도 세션 진입 자체는 유지되므로 10%를 보존.
            slots = empty_slots()
            slots["initial_query"] = True
            state["messages"] = []
            state["user_query"] = ""
            state["keyword_candidates"] = None
            state["keyword_mode"] = None

        else:
            # select_kw:* 옵션은 루프 후 한꺼번에 처리 (edit=교체, add=추가)
            _selected_kws: list[str] = []

            for opt_value in opts:

                # ── 시나리오 A: 슬롯 직접 매핑 ──────────────────────────
                if opt_value in ("연구주제탐색", "논문작성참고", "랩미팅발표", "최신트렌드", "기타"):
                    slots["research_purpose"] = opt_value
                elif opt_value in ("KCI", "SCI", "ALL", "ANY"):
                    slots["paper_scope"] = opt_value
                elif opt_value in ("3Y", "5Y", "10Y", "YEAR_ALL", "SKIP"):
                    slots["pub_year_range"] = opt_value

                # ── 시나리오 A: 슬롯 값 변경 확정 (conflict 질문 후 확인) ─
                elif opt_value.startswith("confirm_change:"):
                    _, slot, new_val = opt_value.split(":", 2)
                    valid_vals = _VALID_SLOT_VALUES.get(slot)
                    if valid_vals and new_val in valid_vals:
                        slots[slot] = new_val

                # ── 시나리오 A: 슬롯 병합 — paper_scope만 지원 ──────────
                elif opt_value.startswith("merge:"):
                    target_slot = opt_value.split(":", 1)[1]
                    if target_slot == "paper_scope":
                        slots["paper_scope"] = "ALL"
                    # 다른 슬롯 merge는 미구현

                # ── GAP-8: 개별 키워드 선택 (edit/add 모드에서 사용) ─────
                elif opt_value.startswith("select_kw:"):
                    _selected_kws.append(opt_value[len("select_kw:"):])

                # ── 키워드 확정: 슬롯 유지, 모드·후보 초기화 ─────────────
                elif opt_value == "confirm_keywords":
                    state["keyword_mode"] = None
                    state["keyword_candidates"] = None

                # ── GAP-8: 키워드 수정/추가 모드 세팅 ───────────────────
                elif opt_value == "edit_keywords":
                    state["keyword_mode"] = "edit"
                elif opt_value == "add_keywords":
                    state["keyword_mode"] = "add"

                # ── 탐색 시작 ─────────────────────────────────────────────
                elif opt_value == "start_search":
                    state["search_ready"] = True

                # ── 시나리오 C: 현재 슬롯 유지, 대화 계속 ──────────────
                elif opt_value in ("keep_topic", "continue"):
                    pass  # 상태 변경 없음

                # ── 낮은 completeness → 연구 목적 질문 유도 (그래프가 처리)
                elif opt_value == "tell_purpose":
                    pass  # 그래프가 research_purpose 빈 슬롯을 감지해 질문

            # ── select_kw 후처리: edit=슬롯 교체, add=기존에 추가 ─────────
            # keywords 슬롯은 묶음 30%: 비어있지 않으면 개수 무관하게 충족.
            if _selected_kws:
                mode = state.get("keyword_mode")
                if mode == "add":
                    existing = list(slots.get("keywords") or [])
                    for kw in _selected_kws:
                        if kw not in existing:
                            existing.append(kw)
                    slots["keywords"] = existing
                else:  # edit 또는 None: 슬롯 전체 교체
                    slots["keywords"] = _selected_kws
                state["keyword_mode"] = None
                state["keyword_candidates"] = None

        state["slots"] = slots

    # LangGraph 실행
    graph = get_graph()
    result_state: SearchState = await graph.ainvoke(state)

    # 상태 저장
    _save_state(session_id, result_state)

    slots = result_state.get("slots") or empty_slots()
    preview = result_state.get("search_preview") or build_search_preview(slots, state["user_query"])

    return success_response(
        data=ChatResponse(
            session_id=session_id,
            ai_message=result_state.get("ai_message", ""),
            options=result_state.get("options") or [],
            allow_multiple=result_state.get("allow_multiple", False),
            search_preview=preview,
            search_ready=result_state.get("search_ready", False),
            completeness_pct=calc_completeness(slots),
            search_stage=_completeness_stage(calc_completeness(slots)),
            final_search_params=result_state.get("final_search_params"),
        ),
        message="chat processed",
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
