from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.response import success_response
from app.core.settings import settings
from app.langgraph.search_graph import get_graph
from app.langgraph.search_state import (
    SearchState,
    _apply_filter_update,
    _apply_keyword_addition,
    empty_filters,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.search import SearchPapersRequest, SearchPapersResponse
from app.services.search_service import search_papers_service

router = APIRouter()

_REDIS_DB = 7
_STATE_TTL = 3600

_CHIP_TYPE_TO_STEP_TYPE = {
    "year": "narrow",
    "paper_type": "narrow",
    "citation": "narrow",
    "expand": "expand",
}


# ── 기존 엔드포인트 (유지) ──────────────────────────────────────────────

@router.post(
    "/search/papers",
    response_model=ApiResponse[SearchPapersResponse],
    responses={422: {"model": ApiErrorResponse}, 500: {"model": ApiErrorResponse}},
    summary="논문 검색 (단순)",
    description="query + 필터로 즉시 검색. 대화 없이 바로 결과 반환.",
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


# ── 검색-먼저 모델 대화 ──────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: Optional[str] = Field(None, description="세션 ID. 첫 턴은 null, 이후 응답의 session_id 재사용")
    message: str = Field("", description="자유입력 텍스트. 첫 턴은 검색 주제, 이후 턴은 좁히기/확장/자유질문. 칩 클릭 시 빈 문자열 가능")
    chip_id: Optional[str] = Field(None, description="칩 클릭 시 응답의 narrow_chips/expand_chips[].chip_id 전달")
    chip_type: Optional[str] = Field(None, description="'year'|'paper_type'|'citation'|'expand' — chip_id와 함께 전달")


class ChatResponse(BaseModel):
    session_id: str = Field(description="세션 ID. 다음 턴 요청 시 재사용")
    filters: Dict[str, Any] = Field(description="현재 누적된 필터. pub_year_start/paper_type/citation_min/keywords")
    sort_order: str = Field(description="정렬 기준. 'relevance' | 'year_desc' | 'citation_desc' 등")
    history: List[Dict[str, Any]] = Field(description="탐색 경로 (step_type/applied_filter/added_keyword/result_count/timestamp)")
    result_items: List[Dict[str, Any]] = Field(description="검색 결과 논문 목록")
    total_count: int = Field(description="전체 결과 수")
    narrow_chips: List[Dict[str, Any]] = Field(description="좁히기 칩. 결과 4편 이하면 빈 배열")
    expand_chips: List[Dict[str, Any]] = Field(description="확장 칩")
    ai_summary: Optional[str] = Field(None, description="LLM 결과 요약. 요약 생성 실패 시 null")
    summary_failed: bool = Field(description="true면 검색은 성공했으나 요약 생성만 실패 (전체 실패 아님)")
    fallback: Optional[str] = Field(None, description="'clarify'|'no_result'|'topic_change'|'off_topic'|null")
    is_broad_result: bool = Field(description="광범위 질문 판정. 임계값(search_broad_result_threshold) 미설정 시 항상 false")


def _load_state(session_id: str) -> Optional[SearchState]:
    r = get_redis(_REDIS_DB)
    raw = r.get(f"search_state:{session_id}")
    if not raw:
        return None
    return json.loads(raw)


def _save_state(session_id: str, state: SearchState) -> None:
    persisted = dict(state)
    persisted.pop("_skip_classification", None)  # 휘발성 신호 — 절대 영속화하지 않음
    r = get_redis(_REDIS_DB)
    r.set(f"search_state:{session_id}", json.dumps(persisted, ensure_ascii=False), ex=_STATE_TTL)


def _new_state(session_id: str, user_query: str) -> SearchState:
    return SearchState(
        user_query=user_query,
        session_id=session_id,
        user_id="",
        sort_order="relevance",
        research_purpose_class=None,
        filters=empty_filters(),
        history=[],
        result_items=[],
        total_count=0,
        type_distribution={},
        narrow_chips=[],
        expand_chips=[],
        ai_summary=None,
        summary_failed=False,
        fallback=None,
        is_broad_result=False,
        messages=[],
    )


def _find_chip(state: SearchState, chip_id: str, chip_type: str) -> Optional[dict]:
    pool = state.get("narrow_chips") or [] if chip_type != "expand" else state.get("expand_chips") or []
    for chip in pool:
        if chip.get("chip_id") == chip_id:
            return chip
    return None


def _apply_chip_action(state: SearchState, chip_id: str, chip_type: str) -> SearchState:
    """칩 클릭 처리 — LLM 호출 없이 코드 레벨에서 확정된 값으로 filters/history 갱신.
    _skip_classification=True로 세팅해 그래프가 분류 노드 없이 response_builder로 바로 진입하게 한다."""
    chip = _find_chip(state, chip_id, chip_type)
    if chip is None:
        logger.warning("칩을 찾을 수 없음: chip_id=%r chip_type=%r", chip_id, chip_type)
        return {**state, "_skip_classification": True}

    filters = dict(state.get("filters") or empty_filters())
    history = list(state.get("history") or [])
    step_type = _CHIP_TYPE_TO_STEP_TYPE.get(chip_type, "narrow")

    if chip_type == "expand":
        keyword = chip.get("keyword")
        filters = dict(_apply_keyword_addition(filters, keyword))
        history.append({
            "step_type": step_type, "applied_filter": None, "added_keyword": keyword,
            "result_count": 0, "timestamp": "",
        })
    else:
        value = chip.get("value") or {}
        filters = dict(_apply_filter_update(filters, value))
        history.append({
            "step_type": step_type, "applied_filter": value, "added_keyword": None,
            "result_count": 0, "timestamp": "",
        })

    return {**state, "filters": filters, "history": history, "_skip_classification": True}


def _sse(event_type: str, payload: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **payload}, ensure_ascii=False)}\n\n"


def _to_chat_response(session_id: str, state: SearchState) -> ChatResponse:
    return ChatResponse(
        session_id=session_id,
        filters=dict(state.get("filters") or empty_filters()),
        sort_order=state.get("sort_order", "relevance"),
        history=list(state.get("history") or []),
        result_items=list(state.get("result_items") or []),
        total_count=state.get("total_count", 0),
        narrow_chips=list(state.get("narrow_chips") or []),
        expand_chips=list(state.get("expand_chips") or []),
        ai_summary=state.get("ai_summary"),
        summary_failed=state.get("summary_failed", False),
        fallback=state.get("fallback"),
        is_broad_result=state.get("is_broad_result", False),
    )


@router.post(
    "/search/chat",
    response_model=ApiResponse[ChatResponse],
    summary="자연어 검색 — 검색-먼저 모델",
    description="""질의 입력 즉시 광범위 검색을 실행하고, 결과 기반 칩/자유입력으로 정교화하는 방식입니다.

**사용 순서**
1. **첫 턴** — `session_id: null`, `message`에 검색 주제 입력
2. **이후 턴** — 응답의 `session_id` 재사용. 자유입력이면 `message`, 칩 클릭이면 `chip_id`+`chip_type` 전달
3. `narrow_chips`/`expand_chips`의 `chip_id`를 다음 턴 `chip_id`에 그대로 전달하면 해당 칩이 적용됨

**칩 클릭 시 주의**: `chip_id`+`chip_type`을 전달하면 `message`는 무시되고 칩 값이 바로 필터에 반영됩니다 (LLM 호출 없음).
""",
)
async def chat_search(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    session_id = request.session_id or str(uuid.uuid4())
    state = _load_state(session_id) or _new_state(session_id, request.message)

    if request.message:
        state["messages"] = (state.get("messages") or []) + [{"role": "user", "content": request.message}]
    state["user_query"] = state.get("user_query") or request.message

    if request.chip_id and request.chip_type:
        state = _apply_chip_action(state, request.chip_id, request.chip_type)

    graph = get_graph()
    try:
        result_state: SearchState = await asyncio.wait_for(
            graph.ainvoke(state),
            timeout=settings.graph_timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="요청 시간이 초과됐어요. 다시 시도해주세요.")

    _save_state(session_id, result_state)
    return success_response(data=_to_chat_response(session_id, result_state), message="chat processed")


@router.post(
    "/search/chat/stream",
    summary="자연어 검색 — SSE 스트리밍",
    description="""`/search/chat`과 동일한 검색-먼저 대화를 SSE(Server-Sent Events)로 스트리밍합니다.

⚠️ POST 방식이므로 `EventSource`(GET 전용) 사용 불가. `fetch` + `ReadableStream` 필요.

**SSE 이벤트 순서**
1. `search_started` — 검색 시작
2. `searching` — 검색 진행 중
3. `papers_found` `{count}` — 발견 논문 건수
4. `fetching` — 논문 상세/요약 가져오는 중
5. `token` — `ai_summary` 청크 스트리밍 (서버 chunking)
6. `done` — 최종 상태 전체 (`ChatResponse`와 동일 필드)

`error` — 예외/타임아웃 시 `done` 없이 스트림 즉시 종료.
""",
)
async def stream_chat(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    session_id = request.session_id or str(uuid.uuid4())

    async def generate():
        try:
            state = _load_state(session_id) or _new_state(session_id, request.message)
            if request.message:
                state["messages"] = (state.get("messages") or []) + [{"role": "user", "content": request.message}]
            state["user_query"] = state.get("user_query") or request.message

            if request.chip_id and request.chip_type:
                state = _apply_chip_action(state, request.chip_id, request.chip_type)

            yield _sse("search_started", {})
            yield _sse("searching", {})

            graph = get_graph()
            try:
                result_state: SearchState = await asyncio.wait_for(
                    graph.ainvoke(state),
                    timeout=settings.graph_timeout_seconds,
                )
            except asyncio.TimeoutError:
                yield _sse("error", {"message": "요청 시간이 초과됐어요. 다시 시도해주세요."})
                return

            _save_state(session_id, result_state)

            yield _sse("papers_found", {"count": result_state.get("total_count", 0)})
            yield _sse("fetching", {})

            ai_summary = result_state.get("ai_summary") or ""
            for i in range(0, len(ai_summary), settings.sse_chunk_size):
                yield _sse("token", {"text": ai_summary[i:i + settings.sse_chunk_size]})
                await asyncio.sleep(settings.sse_chunk_delay_seconds)

            response = _to_chat_response(session_id, result_state)
            yield _sse("done", response.model_dump())

        except Exception:
            logger.exception("SSE 스트리밍 오류 session_id=%s", session_id)
            yield _sse("error", {"message": "서버 오류가 발생했어요. 잠시 후 다시 시도해주세요."})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── 피드백 ────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    session_id: str = Field(description="피드백을 남길 세션 ID")
    paper_id: str = Field(description="피드백 대상 논문 ID")
    feedback: Literal["like", "dislike"] = Field(description="피드백 유형. 'like' | 'dislike'")


_FEEDBACK_KEY = "search_feedback:{session_id}"


@router.post(
    "/search/feedback",
    response_model=ApiResponse[dict],
    summary="논문 좋아요/싫어요",
    description="검색 결과 또는 대화 중 논문에 대한 피드백. session 당 paper별로 Redis에 저장.",
)
async def submit_feedback(request: FeedbackRequest):
    r = get_redis(_REDIS_DB)
    key = _FEEDBACK_KEY.format(session_id=request.session_id)
    existing = json.loads(r.get(key) or "{}")
    existing[request.paper_id] = request.feedback
    r.set(key, json.dumps(existing, ensure_ascii=False), ex=_STATE_TTL)
    return success_response(data={"ok": True}, message="feedback recorded")


@router.get(
    "/search/feedback/{session_id}",
    response_model=ApiResponse[dict],
    summary="세션 피드백 조회",
    description="session_id로 저장된 전체 피드백 목록 반환. {paper_id: 'like'|'dislike'}",
)
async def get_feedback(session_id: str):
    r = get_redis(_REDIS_DB)
    data = json.loads(r.get(_FEEDBACK_KEY.format(session_id=session_id)) or "{}")
    return success_response(data=data, message="ok")
