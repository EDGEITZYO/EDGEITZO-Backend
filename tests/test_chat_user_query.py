"""채팅 세션의 user_query 승계 규칙 테스트.

첫 발화가 검색어가 아니면("안녕?") 키워드가 안 잡히고, 그 세션은 filters.keywords도
history도 비어 있어 다음 턴도 최초 검색 경로(intent_extractor)로 다시 들어간다.
그 경로는 messages가 아니라 user_query를 읽는데, 예전에는 user_query를 첫 값으로
고정해서("state.get('user_query') or request.message") 이후 어떤 검색어를 넣어도
"안녕?"이 계속 재사용됐다 — 한 번 애매하게 시작한 세션은 영구히 막혔다.

반대로 검색이 성립한 뒤의 좁히기 턴("2022년 것만")으로 user_query가 덮이면
요약 문장의 주제와 검색 기록 제목이 그 말로 바뀐다. 두 경우를 가르는 게 이 규칙이다.
"""
from app.api.v1.search import _new_state, _set_user_query


def _started_state(user_query: str):
    """검색이 한 번 성립한 세션 (키워드 + history 있음)"""
    state = _new_state("sid", user_query)
    state["filters"] = {**state["filters"], "keywords": ["노화", "노인의학"]}
    state["history"] = [{"step_id": "s1", "step_type": "search"}]
    return state


def test_첫_검색이_성립하지_않은_세션은_이번_턴_입력으로_갱신된다():
    state = _new_state("sid", "안녕?")
    state = _set_user_query(state, "노화 관련 연구 찾아줘")
    assert state["user_query"] == "노화 관련 연구 찾아줘"


def test_막힌_세션은_몇_턴_뒤에_제대로_된_검색어를_넣어도_복구된다():
    state = _new_state("sid", "안녕?")
    for msg in ("뭐해?", "ㅋㅋ", "치매 조기진단 논문 보여줘"):
        state = _set_user_query(state, msg)
    assert state["user_query"] == "치매 조기진단 논문 보여줘"


def test_검색이_성립한_세션은_좁히기_턴으로_덮이지_않는다():
    state = _started_state("노화 관련 연구 찾아줘")
    state = _set_user_query(state, "2022년 것만 보여줘")
    assert state["user_query"] == "노화 관련 연구 찾아줘"


def test_message가_없는_턴은_그대로_둔다():
    """칩 클릭·필터 패널 조작은 message가 빈 문자열로 온다."""
    state = _new_state("sid", "치매 연구")
    state = _set_user_query(state, "")
    assert state["user_query"] == "치매 연구"


def test_history만_있어도_성립한_세션으로_본다():
    """주제변경 직후처럼 키워드가 비어 있어도 history가 있으면 자유입력 경로로 간다 —
    그 경로는 messages를 읽으므로 user_query를 건드릴 이유가 없다."""
    state = _new_state("sid", "노화 관련 연구 찾아줘")
    state["history"] = [{"step_id": "s1", "step_type": "search"}]
    state = _set_user_query(state, "2022년 것만 보여줘")
    assert state["user_query"] == "노화 관련 연구 찾아줘"
