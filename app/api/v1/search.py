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
        question_count=0,
        search_preview=preview,
        final_search_params=None,
        search_ready=False,
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
        slots = state.get("slots") or empty_slots()
        for opt_value in request.selected_options:
            if opt_value in ("연구주제탐색", "논문작성참고", "랩미팅발표", "최신트렌드", "기타"):
                slots["research_purpose"] = opt_value
            elif opt_value in ("KCI", "SCI", "ALL", "ANY"):
                slots["paper_scope"] = opt_value
            elif opt_value in ("3Y", "5Y", "10Y", "SKIP"):
                slots["pub_year_range"] = opt_value
            elif opt_value == "confirm_keywords":
                pass  # 키워드는 이미 슬롯에 있음
            elif opt_value == "start_search":
                state["search_ready"] = True
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
