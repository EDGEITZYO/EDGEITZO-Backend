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
    SearchParams,
    SearchPreview,
    SearchState,
    build_search_preview,
    calc_completeness,
    empty_slots,
)
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.search import SearchPapersRequest, SearchPapersResponse, SearchParamsDoc
from app.services.credibility_service import paper_type_label, resolve_paper_type
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
    summary="논문 검색 (단순)",
    description="query + 필터로 즉시 검색. 슬롯 대화 없이 바로 결과 반환.",
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
    session_id: Optional[str] = Field(None, description="세션 ID. 첫 턴은 null, 이후 응답의 session_id 재사용")
    message: str = Field(description="사용자 메시지. 첫 턴은 검색 주제, 이후 턴은 자유 입력 또는 빈 문자열 가능", example="딥러닝 관련 최신 논문 찾아줘")
    selected_options: Optional[List[str]] = Field(None, description="이전 응답의 options[].value 목록. 버튼 선택 시 전달", example=["KCI", "3Y"])
    force_start: bool = Field(False, description="true 시 슬롯 완성 여부와 무관하게 검색 즉시 시작")


class InterimPaper(BaseModel):
    paper_id: str = Field(description="논문 고유 ID")
    title: str = Field(description="논문 제목")
    journal: Optional[str] = Field(None, description="학술지명")
    pub_year: Optional[int] = Field(None, description="발행 연도")
    paper_type: Optional[str] = Field(None, description="논문 유형. '박사학위 논문' | '석사학위 논문' | '학위논문' | '학술 저널' | null")
    keywords: List[str] = Field(description="논문 키워드 (최대 4개)")
    scope_badge: Optional[str] = Field(None, description="논문 범위 뱃지. 'KCI' | null")


class FeedbackRequest(BaseModel):
    session_id: str = Field(description="피드백을 남길 세션 ID")
    paper_id: str = Field(description="피드백 대상 논문 ID")
    feedback: Literal["like", "dislike"] = Field(description="피드백 유형. 'like' | 'dislike'")


_FEEDBACK_KEY = "search_feedback:{session_id}"
_INTERIM_THRESHOLD = 60  # completeness_pct >= 이 값이면 interim_papers 조회


class SearchProgress(BaseModel):
    percent: int = Field(description="검색 구체화 진행률 (0~100)")
    status: str = Field(description="진행 상태. 'pending'(<60%) | 'in_progress'(60~99%) | 'complete'(100%)")


class ChatResponse(BaseModel):
    session_id: str = Field(description="세션 ID. 다음 턴 요청 시 재사용")
    turn: int = Field(description="현재 대화 턴 수 (AI 응답 기준)")
    ai_message: str = Field(description="AI 응답 메시지")
    response_type: str = Field(description="UI 분기용. 'options': 버튼 선택 / 'free_input': 텍스트 입력 / 'confirm': 검색 시작 확인")
    options: List[Dict[str, Any]] = Field(description="선택 옵션 목록. 각 항목의 value를 다음 턴 selected_options에 전달")
    allow_multiple: bool = Field(description="true 시 options 다중 선택 가능")
    search_preview: SearchPreview = Field(description="현재까지 채워진 검색 조건 미리보기")
    search_ready: bool = Field(description="true 시 검색 실행 가능 상태. final_search_params를 /search/execute에 전달")
    search_progress: SearchProgress = Field(description="검색 구체화 진행 상태")
    search_stage: str = Field(description="버튼 활성화 분기용. 'none'(<80%) / 'ready'(80~89%) / 'emphasized'(90~99%) / 'complete'(100%)")
    interim_papers: List[InterimPaper] = Field(default=[], description="키워드 추출 완료(60% 이상) 후 미리 보여줄 논문. search_ready=true(80% 이상)이면 최대 20건, 60~79%이면 최대 5건. slots의 paper_scope(KCI/SCI)·pub_year_range 필터 적용")
    final_search_params: Optional[SearchParams] = Field(None, description="search_ready=true일 때 채워짐. /search/execute의 search_params에 그대로 전달")


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


def _response_type(options: list, allow_multiple: bool, search_ready: bool) -> str:
    if options:
        return "options"
    if search_ready:
        return "confirm"
    return "free_input"


def _search_progress(pct: int) -> SearchProgress:
    if pct >= 100:
        status = "complete"
    elif pct >= 60:
        status = "in_progress"
    else:
        status = "pending"
    return SearchProgress(percent=pct, status=status)


def _turn_count(messages: list) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant")


@router.post(
    "/search/chat",
    response_model=ApiResponse[ChatResponse],
    summary="AI 슬롯 대화 — 1턴씩 호출",
    description="""AI와 대화하며 검색 조건을 채워나가는 슬롯 방식 검색입니다.

**사용 순서**
1. **첫 턴** — `session_id: null`, `message`에 검색 주제 입력 (**주의: 첫 턴의 `session_id`는 따옴표 없이 `null`로 전달, 이후 턴부터는 `"session_id"` 처럼 따옴표 포함 문자열로 전달**)
2. **이후 턴** — 응답의 `session_id` 재사용, `selected_options`에 `options[].value` 전달
3. **검색 실행** — `search_ready: true`가 되면 `final_search_params`를 `/search/execute`로 전달

**응답 필드 활용**
- `response_type`: `"options"` → 버튼 선택 UI / `"free_input"` → 텍스트 입력 / `"confirm"` → 확인 버튼
- `completeness_pct`: 0~100, 검색 구체화 진행률 (80% 이상이면 검색 가능 상태)
- `search_stage`: `"none"` / `"ready"` / `"emphasized"` / `"complete"` — 버튼 활성화 분기용
- `interim_papers`: 키워드 추출 완료 후(60% 이상) 미리 보여줄 논문. `search_ready=true`(80% 이상)이면 최대 20건, 60~79%이면 최대 5건
- `options[].value`: 다음 턴 `selected_options`에 그대로 전달

**selected_options 전체 value 목록** (단일 선택이어도 배열로 전달: `["KCI"]`)

슬롯 값:
- 연구 목적: `"연구주제탐색"` `"논문작성참고"` `"랩미팅발표"` `"최신트렌드"`
- 논문 범위: `"KCI"` `"SCI"` `"ALL"` `"ANY"`
- 발행 연도: `"3Y"` `"5Y"` `"10Y"` `"YEAR_ALL"` `"SKIP"`

검색 제어:
- `"start_search"` — 논문 탐색 시작
- `"force_start"` — 80% 미만 상태에서 강제 탐색
- `"restart"` — 처음부터 다시 시작 (슬롯 전체 초기화)

키워드 제어:
- `"confirm_keywords"` — 추출된 키워드 확정
- `"edit_keywords"` — 키워드 수정 (후보 5개 다시 제시)
- `"add_keywords"` — 키워드 추가 입력 유도
- `"select_kw:{키워드명}"` — 후보 중 특정 키워드 선택 (예: `"select_kw:딥러닝"`)

주제/충돌 처리:
- `"keep_topic"` — 현재 주제로 계속 탐색 (주제 변경 확인 시)
- `"new_topic"` — 새 주제로 처음부터 검색 (키워드만 초기화, session 유지)
- `"continue"` — 탐색 계속 (off-topic 복귀 시)
- `"keep"` — 슬롯 충돌 시 기존 값 유지
- `"confirm_change:{slot}:{value}"` — 슬롯 충돌 시 새 값으로 변경 (예: `"confirm_change:paper_scope:SCI"`)
- `"merge:{slot}"` — 슬롯 충돌 시 둘 다 포함 (예: `"merge:paper_scope"`)
- `"tell_purpose"` — 연구 목적 입력 유도로 이동
- `"narrow_field"` — 키워드 추출 실패 시 분야 좁히기 유도

**타임아웃**
- LLM 호출: 120초
- 전체 그래프 실행: 300초 초과 시 HTTP 504 반환
""",
)
async def chat_search(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    state = _load_state(session_id) or _new_state(session_id, request.message)

    state["messages"].append({"role": "user", "content": request.message})
    state["user_query"] = state["user_query"] or request.message
    if request.force_start:
        state["messages"].append({"role": "user", "content": "force_start"})

    state = _apply_selected_options(session_id, state, request.selected_options)

    graph = get_graph()
    try:
        result_state: SearchState = await asyncio.wait_for(
            graph.ainvoke(state),
            timeout=settings.graph_timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="요청 시간이 초과됐어요. 다시 시도해주세요.")
    _save_state(session_id, result_state)

    slots = result_state.get("slots") or empty_slots()
    preview = result_state.get("search_preview") or build_search_preview(slots, state["user_query"])
    pct = calc_completeness(slots)

    opts   = result_state.get("options") or []
    multi  = result_state.get("allow_multiple", False)
    ready  = result_state.get("search_ready", False)

    # interim_papers: 키워드 추출 후(completeness>=60) 미리 노출
    # search_ready(>=80%): 20건 / 60~79%: 힌트용 5건
    interim: list[InterimPaper] = []
    if pct >= _INTERIM_THRESHOLD:
        kws = slots.get("keywords") or []
        search_ready = result_state.get("search_ready", False)
        if kws:
            try:
                from app.repositories.paper_repository import get_paper_cards_batch
                from app.services.chroma_search_service import get_chroma_search_service
                from app.services.paper_filter_service import _YEAR_CUTOFF
                n = 20 if search_ready else 5
                raw_scope = slots.get("paper_scope") or None
                scope = raw_scope if raw_scope not in (None, "ANY", "ALL") else None
                year_range = slots.get("pub_year_range") or None
                pub_year_start = _YEAR_CUTOFF.get(year_range) if year_range else None
                svc = get_chroma_search_service()
                items = await svc.search(query=" ".join(kws), n_results=n, scope=scope, pub_year_start=pub_year_start)
                db_extra = await get_paper_cards_batch(db, [it.paper_id for it in items])
                for it in items:
                    db_c = it.db_code or ""
                    extra = db_extra.get(it.paper_id, {})
                    degree = extra.get("degree")
                    kci = extra.get("kci_registered", db_c == "JAKO")
                    interim.append(InterimPaper(
                        paper_id=it.paper_id,
                        title=it.title,
                        journal=it.journal_name,
                        pub_year=it.year,
                        paper_type=paper_type_label(resolve_paper_type(db_c or None, degree)),
                        keywords=it.keywords[:4],
                        scope_badge="KCI" if kci else None,
                    ))
            except Exception as e:
                import traceback
                print(f"[_fetch_interim ERROR] {e}")
                traceback.print_exc()

    messages = result_state.get("messages") or []
    return success_response(
        data=ChatResponse(
            session_id=session_id,
            turn=_turn_count(messages),
            ai_message=result_state.get("ai_message", ""),
            response_type=_response_type(opts, multi, ready),
            options=opts,
            allow_multiple=multi,
            search_preview=preview,
            search_ready=ready,
            search_progress=_search_progress(pct),
            search_stage=_completeness_stage(pct),
            interim_papers=interim,
            final_search_params=result_state.get("final_search_params"),
        ),
        message="chat processed",
    )


@router.post(
    "/search/chat/stream",
    summary="AI 슬롯 대화 — SSE 스트리밍",
    description="""`/search/chat`과 동일한 슬롯 대화를 SSE(Server-Sent Events)로 스트리밍합니다.

⚠️ POST 방식이므로 `EventSource`(GET 전용) 사용 불가. `fetch` + `ReadableStream` 필요.

**SSE 수신 방법 (fetch + ReadableStream)**
```js
const res = await fetch('/api/v1/search/chat/stream', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ session_id, message, selected_options }),
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  const lines = decoder.decode(value).split('\\n');
  for (const line of lines) {
    if (line.startsWith('data: ')) {
      const event = JSON.parse(line.slice(6));
      // event.type으로 분기
    }
  }
}
```

**SSE 이벤트 순서 및 payload 구조**

1. `slot_update` — selected_options 처리 후 변경된 슬롯 (슬롯별 1회, 복수 emit 가능)
```json
{"type": "slot_update", "slot": "paper_scope", "value": "KCI"}
```
slot: `"research_purpose"` | `"paper_scope"` | `"pub_year_range"` | `"keywords"`

2. `completeness` — options 처리 직후 구체화도
```json
{"type": "completeness", "pct": 60, "stage": "none"}
```
stage: `"none"`(<80%) | `"ready"`(80~89%) | `"emphasized"`(90~99%) | `"complete"`(100%)

3. `keyword_progress` — 키워드 추출 시작 (키워드 슬롯 비어있을 때, 또는 `edit_keywords`/`add_keywords` 선택 시 emit)
```json
{"type": "keyword_progress", "stage": "started"}
```

4. _(LLM + 그래프 실행 — 블로킹 구간)_

5. `slot_update` — 그래프 실행 후 변경된 슬롯 (1~4번과 동일 구조)

6. `completeness` — 그래프 실행 후 업데이트된 구체화도 (2번과 동일 구조)

7. `keyword_progress` — 키워드 추출 완료
```json
{"type": "keyword_progress", "stage": "completed", "keywords": ["딥러닝", "CNN"]}
```

8. `token` — ai_message 10자씩 청크 (서버 chunking, LLM 스트리밍 아님)
```json
{"type": "token", "text": "입력하신 내용에"}
```

9. `done` — 최종 상태 전체
```json
{
  "type": "done",
  "session_id": "abc123",
  "ai_message": "전체 AI 메시지",
  "options": [{"label": "이대로 검색", "value": "confirm_keywords"}],
  "allow_multiple": false,
  "search_ready": false,
  "completeness_pct": 70,
  "search_stage": "none",
  "search_preview": {"topic": "...", "purpose": null, "scope": null, "pub_year": null, "keywords": [], "completeness_pct": 70},
  "interim_papers": [{"paper_id": "JAKO...", "title": "...", "journal": null, "pub_year": 2023, "paper_type": "학술 저널", "keywords": ["딥러닝"], "scope_badge": "KCI"}],
  "final_search_params": null
}
```

`error` — 예외 발생 시 또는 타임아웃 시. done 없이 스트림 즉시 종료
```json
{"type": "error", "message": "서버 오류가 발생했어요. 잠시 후 다시 시도해주세요."}
```
타임아웃 시:
```json
{"type": "error", "message": "요청 시간이 초과됐어요. 다시 시도해주세요."}
```

**타임아웃**
- LLM 호출: 120초
- 전체 그래프 실행: 300초 초과 시 error 이벤트 emit 후 스트림 종료
""",
)
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
    error — 예외 발생 시 또는 graph 타임아웃(300초) 시. done 없이 스트림 즉시 종료.
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
            try:
                result_state: SearchState = await asyncio.wait_for(
                    graph.ainvoke(state),
                    timeout=settings.graph_timeout_seconds,
                )
            except asyncio.TimeoutError:
                yield _sse("error", {"message": "요청 시간이 초과됐어요. 다시 시도해주세요."})
                return
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

            # interim_papers 조회를 token 스트리밍과 병렬 실행
            _search_ready = result_state.get("search_ready", False)
            _interim_scope = slots_final.get("paper_scope") or None
            _interim_year = slots_final.get("pub_year_range") or None

            async def _fetch_interim(kws: list[str]) -> list[dict]:
                try:
                    from app.repositories.paper_repository import get_paper_cards_batch
                    from app.services.chroma_search_service import get_chroma_search_service
                    from app.services.paper_filter_service import _YEAR_CUTOFF
                    n = 20 if _search_ready else 5
                    scope = _interim_scope if _interim_scope not in (None, "ANY", "ALL") else None
                    pub_year_start = _YEAR_CUTOFF.get(_interim_year) if _interim_year else None
                    svc = get_chroma_search_service()
                    items = await svc.search(query=" ".join(kws), n_results=n, scope=scope, pub_year_start=pub_year_start)
                    db_extra = await get_paper_cards_batch(db, [it.paper_id for it in items])
                    result = []
                    for it in items:
                        db_c = it.db_code or ""
                        extra = db_extra.get(it.paper_id, {})
                        degree = extra.get("degree")
                        kci = extra.get("kci_registered", db_c == "JAKO")
                        result.append(InterimPaper(
                            paper_id=it.paper_id,
                            title=it.title,
                            journal=it.journal_name,
                            pub_year=it.year,
                            paper_type=paper_type_label(resolve_paper_type(db_c or None, degree)),
                            keywords=it.keywords[:4],
                            scope_badge="KCI" if kci else None,
                        ).model_dump())
                    return result
                except Exception as e:
                    import traceback
                    print(f"[_fetch_interim ERROR] {e}")
                    traceback.print_exc()
                    return []

            kws_for_interim = slots_final.get("keywords") or []
            interim_task = (
                asyncio.create_task(_fetch_interim(kws_for_interim))
                if pct >= _INTERIM_THRESHOLD and kws_for_interim
                else None
            )

            for i in range(0, len(ai_message), settings.sse_chunk_size):
                yield _sse("token", {"text": ai_message[i:i + settings.sse_chunk_size]})
                await asyncio.sleep(settings.sse_chunk_delay_seconds)

            # ── 9. done ───────────────────────────────────────────────────
            preview = (result_state.get("search_preview")
                       or build_search_preview(slots_final, state.get("user_query", "")))

            if interim_task is not None:
                try:
                    interim: list[dict] = await interim_task
                except Exception:
                    interim = []
            else:
                interim = []

            yield _sse("done", {
                "session_id": session_id,
                "ai_message": ai_message,
                "options": result_state.get("options") or [],
                "allow_multiple": result_state.get("allow_multiple", False),
                "search_ready": result_state.get("search_ready", False),
                "completeness_pct": pct,
                "search_stage": _completeness_stage(pct),
                "search_preview": preview,
                "interim_papers": interim,
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


@router.get(
    "/search/stream/{session_id}",
    summary="검색 조건 미리보기 SSE",
    description="""현재 세션의 `search_preview`를 SSE로 반환합니다. LLM 추가 호출 없음.

**이벤트 순서**
1. `preview_update` `{content: SearchPreview}` — 현재 검색 조건 미리보기 (세션 없으면 생략)
2. `done`

세션이 존재하지 않아도 오류 없이 `done`만 반환합니다.
""",
)
async def stream_preview(session_id: str):
    """현재 search_preview를 SSE로 반환 (LLM 추가 호출 없음)"""
    async def generate():
        state = _load_state(session_id)
        if state:
            preview = state.get("search_preview") or {}
            yield f"data: {json.dumps({'type': 'preview_update', 'content': preview}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 피드백 ────────────────────────────────────────────────────────────

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


# ── PART A 실행 검색 ───────────────────────────────────────────────────

class PaperResult(BaseModel):
    paper_id: str = Field(description="논문 고유 ID", example="JAKO202312345678")
    title: str = Field(description="논문 제목")
    authors: List[str] = Field(description="저자 목록", example=["홍길동", "김철수"])
    pub_year: Optional[int] = Field(None, description="발행 연도", example=2023)
    journal: Optional[str] = Field(None, description="학술지명")
    paper_type: Optional[str] = Field(None, description="논문 유형. '박사학위 논문' | '석사학위 논문' | '학위논문' | '학술 저널' | null")
    abstract: Optional[str] = Field(None, description="초록")
    keywords: List[str] = Field(description="논문 키워드")
    doi: Optional[str] = Field(None, description="DOI URL")
    scope_badge: Optional[str] = Field(None, description="논문 범위 뱃지. 'KCI' | null")
    citation_count: Optional[int] = Field(None, description="인용 수")
    relevance_score: float = Field(description="ChromaDB 유사도 점수 (0~1)")
    trust_badge: Optional[str] = Field(None, description="신뢰도 뱃지 (MVP 이후 제공 예정)")
    keyword_map_data: None = Field(None, description="키워드맵 연결 데이터 (MVP 이후 제공 예정)")


class ExecuteRequest(BaseModel):
    session_id: str = Field(description="/search/chat 응답의 session_id")
    search_params: SearchParamsDoc = Field(description="/search/chat 응답의 final_search_params 그대로 전달")
    filter_paper_type: Optional[Literal["학술 저널", "박사학위 논문", "석사학위 논문", "학위논문", "전체"]] = Field(None, description="논문 유형 필터. null 또는 '전체' 시 전체 조회")
    sort_order: Literal["relevance", "year_asc", "year_desc"] = Field("relevance", description="정렬 기준. 'relevance': 유사도 / 'year_asc': 발행일 오름차순 / 'year_desc': 발행일 내림차순")
    user_id: Optional[str] = Field(None, description="검색 이력 저장용 유저 ID (선택). 제공 시 Redis에 AI 검색 이력 저장")


class ExecuteResponse(BaseModel):
    papers: List[PaperResult] = Field(description="논문 결과 목록")
    total: int = Field(description="전체 결과 수")
    search_id: str = Field(description="검색 고유 ID. 피드백 연동 시 사용")


@router.post(
    "/search/execute",
    response_model=ApiResponse[ExecuteResponse],
    summary="슬롯 대화 완료 후 실행 검색",
    description=(
        "`/search/chat`에서 `search_ready: true`가 반환된 이후 실제 논문 검색을 실행하는 엔드포인트입니다.\n\n"
        "**사용 순서**\n"
        "1. `/search/chat`을 반복 호출하여 `search_ready: true` 응답을 받는다\n"
        "2. 해당 응답의 `session_id`와 `final_search_params`를 그대로 이 API의 `session_id`, `search_params`에 전달한다\n"
        "3. 응답의 `papers` 목록을 논문 검색 결과 화면에 표시한다\n\n"
        "**요청 필드**\n"
        "- `session_id` — `/search/chat` 응답의 `session_id` (검색 이력 저장에 사용)\n"
        "- `search_params` — `/search/chat` 응답의 `final_search_params` 그대로 전달\n"
        "- `filter_paper_type` — 논문 유형 필터. `'학술 저널'`|`'박사학위 논문'`|`'석사학위 논문'`|`'학위논문'`|`'전체'`|null (null = 전체)\n"
        "- `sort_order` — 정렬 기준. `'relevance'`(유사도순) | `'year_asc'`(오래된순) | `'year_desc'`(최신순)\n"
        "- `user_id` — 제공 시 Redis에 AI 검색 이력 저장 (선택)\n\n"
        "**응답 필드**\n"
        "- `papers` — 논문 결과 목록\n"
        "- `total` — 전체 결과 수\n"
        "- `search_id` — 검색 고유 ID (피드백 `/search/feedback` 연동 시 사용)"
    ),
)
async def execute_search_endpoint(
    request: ExecuteRequest,
    db: AsyncSession = Depends(get_db),
):
    sp = request.search_params.model_dump()
    result = await execute_search(
        sp,
        db,
        filter_paper_type=request.filter_paper_type,
        sort_order=request.sort_order,
    )

    # 검색 이력 저장 (user_id 제공 시)
    if request.user_id:
        try:
            from app.api.v1.home import save_search_history
            state = _load_state(request.session_id)
            slots = (state or {}).get("slots", {})
            user_query = (state or {}).get("user_query", "")
            kws = sp.get("keywords") or []
            save_search_history(
                user_id=request.user_id,
                search_type="ai",
                title=user_query or " ".join(kws[:2]) or "AI 검색",
                search_id=result["search_id"],
                recommended_keywords=kws,
                slots=dict(slots),
            )
        except Exception:
            pass

    return success_response(
        data=ExecuteResponse(
            papers=[PaperResult(**p) for p in result["papers"]],
            total=result["total"],
            search_id=result["search_id"],
        ),
        message="search executed",
    )
