"""홈 검색어 추천 칩 — 문장 조건·열람 기간·키워드 집계·흐름 분기."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import search_suggestion_service as svc
from app.services.keywords.keyword_db import Keyword

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class TestNormalizeAiQuery:
    def test_24자_이하_한_문장은_통과(self):
        q = "가" * 23 + "?"
        assert svc.normalize_ai_query(q) == q

    def test_공백_포함_24자_초과는_탈락(self):
        assert svc.normalize_ai_query("가 " * 12 + "나") is None

    def test_두_문장은_탈락(self):
        assert svc.normalize_ai_query("논문 찾아줘. 최신으로?") is None

    def test_따옴표와_겹공백은_정리(self):
        assert svc.normalize_ai_query('"논문을   찾고 싶어"') == "논문을 찾고 싶어"

    def test_문자열이_아니면_None(self):
        assert svc.normalize_ai_query(None) is None

    def test_후보_중_조건_맞는_첫_문장(self):
        assert svc._pick_ai_query(["가" * 30, "짧은 문장이야?"]) == "짧은 문장이야?"


class TestSingleWord:
    def test_띄어쓰기_있으면_탈락(self):
        assert not svc.is_single_word("기계 학습")

    def test_한_단어는_통과(self):
        assert svc.is_single_word("유전체")


class TestHistoryWindow:
    def test_최근_2주_안에_열람이_있으면_오늘_기준(self):
        assert svc.history_window(NOW - timedelta(days=3), NOW) == (NOW - timedelta(days=14), NOW)

    def test_최근_2주에_없으면_마지막_열람일_기준(self):
        last = NOW - timedelta(days=40)
        assert svc.history_window(last, NOW) == (last - timedelta(days=14), last)


class TestRankHistoryKeywords:
    def test_열람_수_합으로_정렬하고_논문_안_중복은_한_번(self):
        rows = [
            (["A", "B", "a"], 3, NOW),          # A와 a는 같은 키워드 → 3만 더함
            (["B"], 1, NOW - timedelta(days=1)),
            (["C"], 5, NOW - timedelta(days=2)),
        ]
        ranked = svc.rank_history_keywords(rows)
        assert [(name, views) for name, _, views in ranked] == [("C", 5), ("B", 4), ("A", 3)]

    def test_동점이면_최근에_본_쪽이_앞(self):
        rows = [(["옛것"], 2, NOW - timedelta(days=5)), (["새것"], 2, NOW)]
        assert [n for n, _, _ in svc.rank_history_keywords(rows)] == ["새것", "옛것"]

    def test_키워드_없는_논문은_무시(self):
        assert svc.rank_history_keywords([(None, 1, NOW), ([], 2, NOW)]) == []


def _kw(name):
    return Keyword(key=f"ko:{name.casefold()}", name_ko=name, name_en=None, paper_count=1)


class TestFlows:
    def test_기존_사용자는_LLM이_고른_후보_키워드(self, monkeypatch):
        cands = [(_kw("일"), 5), (_kw("이"), 3)]

        async def fake_llm(prompt, label):
            return {"keyword": "이", "ai_queries": ["가" * 40, "이 분야 논문을 찾고 싶어"]}

        monkeypatch.setattr(svc, "_ask_llm", fake_llm)
        r = asyncio.run(svc._from_history(cands, (NOW - timedelta(days=14), NOW)))
        assert r.basis == "history"
        assert r.keyword.name == "이" and r.keyword.key == "ko:이"
        assert r.ai_query == "이 분야 논문을 찾고 싶어"

    def test_기존_사용자_LLM이_목록_밖을_고르면_1위(self, monkeypatch):
        async def fake_llm(prompt, label):
            return {"keyword": "목록에없음", "ai_queries": []}

        monkeypatch.setattr(svc, "_ask_llm", fake_llm)
        r = asyncio.run(svc._from_history([(_kw("일"), 5)], (NOW, NOW)))
        assert r.keyword.name == "일" and r.ai_query is None

    def test_기존_사용자_LLM_실패해도_키워드는_1위(self, monkeypatch):
        async def fake_llm(prompt, label):
            return None

        monkeypatch.setattr(svc, "_ask_llm", fake_llm)
        r = asyncio.run(svc._from_history([(_kw("일"), 5)], (NOW, NOW)))
        assert r.keyword.name == "일"

    def test_온보딩_연구분야_없으면_none_LLM_호출_안함(self, monkeypatch):
        async def fail(*a, **k):
            raise AssertionError("호출되면 안 됨")

        monkeypatch.setattr(svc, "_ask_llm", fail)
        user = SimpleNamespace(research_field=None, purposes=["최신 트렌드 파악"], purpose_custom=None)
        r = asyncio.run(svc._from_onboarding(user))
        assert r.basis == "none" and r.ai_query is None and r.keyword is None

    def test_온보딩_키워드는_DB에_있는_것만(self, monkeypatch):
        async def fake_llm(prompt, label):
            assert "연구 분야: 테스트분야" in prompt
            return {"ai_queries": ["관련 논문을 찾고 싶어"], "keywords": ["두 단어", "없는말", "있는말"]}

        monkeypatch.setattr(svc, "_ask_llm", fake_llm)
        monkeypatch.setattr(svc, "get_keywords_by_keys", lambda keys: {"ko:있는말": _kw("있는말")})
        user = SimpleNamespace(research_field="테스트분야", purposes=["기타"], purpose_custom="직접 입력")
        r = asyncio.run(svc._from_onboarding(user))
        assert r.basis == "onboarding"
        assert r.keyword.name == "있는말"
        assert r.ai_query == "관련 논문을 찾고 싶어"
