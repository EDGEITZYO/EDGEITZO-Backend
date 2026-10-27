"""발행연도 범위 필터 — 공통 규칙, Chroma where절, AI 탐색 세션 필터."""
from app.api.v1 import search as search_api
from app.api.v1.search import ChatRequest, _apply_direct_filters
from app.langgraph.search_state import _apply_filter_update, _release_panel_ownership, empty_filters
from app.schemas.search import SearchPapersRequest
from app.services.chroma_search_service import _build_where_clause
from app.services.year_range import resolve_year_range, year_in_range


def test_resolve_year_range():
    assert resolve_year_range(year=2023) == (2023, 2023)
    assert resolve_year_range(year=2023, start=2020) == (2020, None)  # 범위가 우선
    assert resolve_year_range(start=2025, end=2023) == (2023, 2025)  # 거꾸로 오면 바로잡음
    assert resolve_year_range() == (None, None)


def test_year_in_range():
    assert year_in_range(2024, 2023, 2025)
    assert year_in_range(2023, 2023, 2025) and year_in_range(2025, 2023, 2025)
    assert not year_in_range(2026, 2023, 2025)
    assert year_in_range(2030, 2023, None)
    assert not year_in_range(None, 2023, None)  # 연도 미상은 범위가 걸리면 제외
    assert year_in_range(None, None, None)


def test_chroma_where_clause():
    assert _build_where_clause(2023, 2025) == {
        "$and": [{"Pubyear": {"$gte": 2023}}, {"Pubyear": {"$lte": 2025}}]
    }
    assert _build_where_clause(2023, None) == {"Pubyear": {"$gte": 2023}}
    assert _build_where_clause(None, None) is None


def test_search_papers_request_legacy_single_year():
    assert SearchPapersRequest(query="q", pub_year_start=2022).year_range() == (2022, 2022)
    assert SearchPapersRequest(query="q", pub_year_start=2022, pub_year_end=None).year_range() == (2022, None)
    assert SearchPapersRequest(query="q", pub_year_start=2022, pub_year_end=2024).year_range() == (2022, 2024)


class TestChatFilterUpdate:
    """LLM 추출·칩 경로 — 연도 양 끝은 한 묶음으로 바뀐다."""

    def test_한쪽만_와도_다른_쪽은_열린다(self):
        f = _apply_filter_update(empty_filters(), {"pub_year_start": 2022, "pub_year_end": 2022})
        f = _apply_filter_update(f, {"pub_year_start": 2020})  # "2020년 이후"
        assert (f["pub_year_start"], f["pub_year_end"]) == (2020, None)

    def test_연도_언급이_없으면_유지(self):
        f = _apply_filter_update(empty_filters(), {"pub_year_start": 2022, "pub_year_end": 2024})
        f = _apply_filter_update(f, {"paper_type": "학술 저널", "pub_year_start": None})
        assert (f["pub_year_start"], f["pub_year_end"]) == (2022, 2024)

    def test_소유권은_양끝이_같이_풀린다(self):
        owned = ["pub_year_start", "pub_year_end", "kci_only"]
        assert _release_panel_ownership(owned, {"pub_year_end": 2020}) == ["kci_only"]


def _state(**filters):
    base = dict(empty_filters())
    base.update(filters)
    return {"filters": base, "panel_owned": [], "history": [], "narrow_chips": [], "expand_chips": []}


class TestDirectYearFilter:
    def test_범위_슬라이더(self):
        req = ChatRequest(session_id="s", pub_year_start=2023, pub_year_end=2025)
        st = _apply_direct_filters(_state(), req)
        assert (st["filters"]["pub_year_start"], st["filters"]["pub_year_end"]) == (2023, 2025)
        assert {"pub_year_start", "pub_year_end"} <= set(st["panel_owned"])

    def test_예전_드롭다운은_그_해만(self):
        st = _apply_direct_filters(_state(), ChatRequest(session_id="s", pub_year_start=2022))
        assert (st["filters"]["pub_year_start"], st["filters"]["pub_year_end"]) == (2022, 2022)

    def test_채팅이_건_범위는_패널_전체로_안_지워진다(self):
        req = ChatRequest(session_id="s", message="KCI만", pub_year_start=None, pub_year_end=None)
        st = _apply_direct_filters(_state(pub_year_start=2023, pub_year_end=2023), req)
        assert (st["filters"]["pub_year_start"], st["filters"]["pub_year_end"]) == (2023, 2023)

    def test_패널이_건_범위는_전체로_해제(self):
        st = _apply_direct_filters(_state(), ChatRequest(session_id="s", pub_year_start=2020, pub_year_end=2021))
        st = _apply_direct_filters(st, ChatRequest(session_id="s", pub_year_start=None, pub_year_end=None))
        assert (st["filters"]["pub_year_start"], st["filters"]["pub_year_end"]) == (None, None)
        assert "pub_year_start" not in st["panel_owned"]

    def test_같은_요청의_칩이_슬라이더보다_우선(self):
        chip = {"chip_id": "narrow_year", "chip_type": "year", "label": "2024년 논문만 보기",
                "value": {"pub_year_start": 2024, "pub_year_end": 2024}}
        state = {**_state(), "narrow_chips": [chip]}
        req = ChatRequest(session_id="s", chip_id="narrow_year", chip_type="year",
                          pub_year_start=2020, pub_year_end=2025)
        st = _apply_direct_filters(state, req)
        st = search_api._apply_chip_action(st, req.chip_id, req.chip_type)
        assert (st["filters"]["pub_year_start"], st["filters"]["pub_year_end"]) == (2024, 2024)
