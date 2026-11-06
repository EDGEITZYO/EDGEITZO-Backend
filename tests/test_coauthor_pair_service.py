"""공저자 페이지(함께 작성한 논문) — 역할 해석·저자 찾기·공동 연구 키워드."""
import asyncio
from types import SimpleNamespace

from app.schemas.researcher_detail import ResearcherProfileResponse
from app.services import coauthor_pair_service as pair_service
from app.services import researcher_detail_service as service
from tests.test_researcher_detail_service import make_row


def person(name="김민준", institution="한국대학교", name_eng=None, rid="kci:a"):
    return ResearcherProfileResponse(
        researcher_id=rid, name_kor=name, name_eng=name_eng, institution=institution
    )


class TestRolesFrom:
    def test_제1저자는_division만_있어도_잡힌다(self):
        # 2015년 이전 논문은 author-part가 비어 오지만 author-division은 온다
        assert service._roles_from("1", None, [None, None]) == ["first"]

    def test_교신이_한_명이면_교신저자(self):
        assert service._roles_from("2", "교신", ["제1", "참여", "교신"]) == ["corresponding"]

    def test_교신이_여러_명이면_공동_교신저자(self):
        parts = ["제1", "교신", "교신"]
        assert service._roles_from("2", "교신", parts) == ["co_corresponding"]

    def test_제1저자가_교신을_겸하면_둘_다(self):
        assert service._roles_from("1", "교신", ["교신", "참여"]) == ["first", "corresponding"]

    def test_참여는_참여저자(self):
        assert service._roles_from("2", "참여", ["제1", "참여"]) == ["participant"]

    def test_역할이_비어도_논문에_교신_표시가_있으면_참여저자(self):
        assert service._roles_from("2", None, ["제1", None, "교신"]) == ["participant"]

    def test_교신_표시가_없는_논문의_나머지_저자는_알_수_없다(self):
        assert service._roles_from("2", None, [None, None]) == []

    def test_단독은_제1저자(self):
        assert service._roles_from(None, "단독", ["단독"]) == ["first"]


class TestFindAuthor:
    def test_이름이_한_번이면_그_자리(self):
        assert service._find_author(person(), ["이준호", "김민준"], [None, None]) == 1

    def test_띄어쓰기가_달라도_찾는다(self):
        assert service._find_author(person(), ["김 민준"], [None]) == 0

    def test_동명이인은_소속으로_가른다(self):
        names = ["김민준", "김민준"]
        insts = ["나라대학교 화학과", "한국대학교 생명공학과"]
        assert service._find_author(person(), names, insts) == 1

    def test_소속으로도_못_가르면_None(self):
        names = ["김민준", "김민준"]
        assert service._find_author(person(), names, ["한국대학교", "한국대학교"]) is None

    def test_한글명이_없으면_영문명으로_찾는다(self):
        p = person(name=None, name_eng="Kim, Minjun")
        assert service._find_author(p, ["Lee", "Kim, Minjun"], [None, None]) == 1


class TestCommonKeywords:
    """함께 쓴 논문들 사이에서 겹치는 키워드. LLM 호출(_ai_ranking)은 가짜로 바꿔 끼운다."""

    PAIR = ("kci:a", "kci:b")

    def run(self, rows, limit=3, ranking=None, monkeypatch=None, calls=None):
        async def fake(db, pair, rows_, chosen, candidates):
            if calls is not None:
                calls.append((chosen, candidates))
            return ranking

        monkeypatch.setattr(pair_service, "_ai_ranking", fake)
        result = asyncio.run(pair_service.common_keywords(None, self.PAIR, rows, limit))
        return [(k.keyword, k.source, k.paper_count) for k in result]

    def test_1편이면_그_논문_키워드_그대로_LLM_안_부름(self, monkeypatch):
        calls = []
        rows = [make_row(keywords=["간", "독성", "오가노이드", "신약"])]
        assert self.run(rows, monkeypatch=monkeypatch, calls=calls) == [
            ("간", "single_paper", 1), ("독성", "single_paper", 1), ("오가노이드", "single_paper", 1)
        ]
        assert calls == []

    def test_겹치는_키워드로_다_차면_LLM_안_부름(self, monkeypatch):
        calls = []
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["오가노이드", "독성", "간"]),
            make_row(external_id="A2", pubyear=2024, keywords=["오가노이드", "독성", "간"]),
        ]
        out = self.run(rows, monkeypatch=monkeypatch, calls=calls)
        assert [s for _, s, _ in out] == ["repeated"] * 3
        assert calls == []

    def test_겹친_편수_많은_순(self, monkeypatch):
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["신경", "오가노이드"]),
            make_row(external_id="A2", pubyear=2024, keywords=["오가노이드", "독성"]),
            make_row(external_id="A3", pubyear=2023, keywords=["오가노이드", "독성"]),
        ]
        out = self.run(rows, limit=2, monkeypatch=monkeypatch)
        assert out == [("오가노이드", "repeated", 3), ("독성", "repeated", 2)]

    def test_모자란_칸만_AI가_후보_중에서_채운다(self, monkeypatch):
        calls = []
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["오가노이드", "신경"]),
            make_row(external_id="A2", pubyear=2024, keywords=["오가노이드", "간독성", "연구"]),
        ]
        out = self.run(rows, ranking=["간독성", "신경"], monkeypatch=monkeypatch, calls=calls)
        assert out == [("오가노이드", "repeated", 2), ("간독성", "ai", 1), ("신경", "ai", 1)]
        # 이미 고른 키워드는 후보에서 빠지고, 프롬프트에는 '이미 고른 키워드'로 넘어간다
        chosen, candidates = calls[0]
        assert chosen == ["오가노이드"] and "오가노이드" not in candidates

    def test_AI가_후보_밖_키워드를_내면_버린다(self, monkeypatch):
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["신경"]),
            make_row(external_id="A2", pubyear=2024, keywords=["간"]),
        ]
        out = self.run(rows, ranking=["지어낸키워드", "간"], monkeypatch=monkeypatch)
        assert out == [("간", "ai", 1)]

    def test_LLM을_못_쓰면_최신_논문_키워드로(self, monkeypatch):
        rows = [
            make_row(external_id="A1", pubyear=2020, keywords=["옛키워드"]),
            make_row(external_id="A2", pubyear=2025, keywords=["새키워드1", "새키워드2"]),
        ]
        out = self.run(rows, ranking=None, monkeypatch=monkeypatch)
        assert out == [("새키워드1", "recent", 1), ("새키워드2", "recent", 1), ("옛키워드", "recent", 1)]

    def test_표기_흔들림은_같은_키워드로_센다(self, monkeypatch):
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["Deep Learning"]),
            make_row(external_id="A2", pubyear=2024, keywords=["deep learning"]),
        ]
        assert self.run(rows, limit=1, monkeypatch=monkeypatch) == [("Deep Learning", "repeated", 2)]

    def test_한_논문_안의_중복은_한_편으로_센다(self, monkeypatch):
        rows = [make_row(external_id="A1", keywords=["간", "간"]), make_row(external_id="A2", keywords=["뇌"])]
        out = self.run(rows, ranking=None, monkeypatch=monkeypatch)
        assert ("간", "recent", 1) in out

    def test_키워드가_없으면_빈_배열(self, monkeypatch):
        rows = [make_row(external_id="A1", keywords=[]), make_row(external_id="A2", keywords=[])]
        assert self.run(rows, monkeypatch=monkeypatch) == []


class TestSameMeaning:
    """고른 키워드에 한글·영문이 섞이면 LLM이 뜻이 같은 것을 묶고, 묶인 것은 한 칸(한글 표기)으로 합친다."""

    def run(self, rows, monkeypatch, limit=3, ranking=None, groups=None, calls=None):
        async def fake_rank(*_):
            if calls is not None:
                calls.append("rank")
            return ranking

        async def fake_groups(db, pair, rows_, keywords):
            if calls is not None:
                calls.append(("groups", keywords))
            return {pair_service._keyword_key(k): gid for gid, g in enumerate(groups or []) for k in g}

        monkeypatch.setattr(pair_service, "_ai_ranking", fake_rank)
        monkeypatch.setattr(pair_service, "_same_meaning_groups", fake_groups)
        result = asyncio.run(pair_service.common_keywords(None, ("kci:a", "kci:b"), rows, limit))
        return [(k.keyword, k.source, k.paper_count) for k in result]

    def test_번역쌍은_한_칸으로_한글_표기_빈칸은_다음_순위(self, monkeypatch):
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["broiler", "육계", "사료", "장내미생물"]),
            make_row(external_id="A2", pubyear=2024, keywords=["broiler", "육계", "사료", "장내미생물"]),
        ]
        out = self.run(rows, monkeypatch, groups=[["육계", "broiler"]])
        assert out == [("육계", "repeated", 2), ("사료", "repeated", 2), ("장내미생물", "repeated", 2)]

    def test_한글만이면_중복_확인을_부르지_않는다(self, monkeypatch):
        calls = []
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["육계", "사료", "간"]),
            make_row(external_id="A2", pubyear=2024, keywords=["육계", "사료", "간"]),
        ]
        self.run(rows, monkeypatch, calls=calls)
        assert calls == []

    def test_묶지_않으면_그대로(self, monkeypatch):
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["육계", "feed"]),
            make_row(external_id="A2", pubyear=2024, keywords=["육계", "feed"]),
        ]
        assert [k for k, _, _ in self.run(rows, monkeypatch, groups=[])] == ["육계", "feed"]

    def test_합치고_모자라면_그때_AI로_채운다(self, monkeypatch):
        calls = []
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["육계", "broiler", "사료", "항생제 대체제"]),
            make_row(external_id="A2", pubyear=2024, keywords=["육계", "broiler", "사료"]),
        ]
        out = self.run(rows, monkeypatch, ranking=["항생제 대체제"], groups=[["육계", "broiler"]], calls=calls)
        assert out == [("육계", "repeated", 2), ("사료", "repeated", 2), ("항생제 대체제", "ai", 1)]
        assert calls[0][0] == "groups" and calls[1] == "rank"

    def test_AI가_고른_번역쌍도_한_칸(self, monkeypatch):
        rows = [
            make_row(external_id="A1", pubyear=2025, keywords=["사료"]),
            make_row(external_id="A2", pubyear=2024, keywords=["사료", "broiler", "육계"]),
        ]
        out = self.run(rows, monkeypatch, ranking=["broiler", "육계"], groups=[["broiler", "육계"]])
        assert out == [("사료", "repeated", 2), ("육계", "ai", 1)]

    def test_1편은_LLM을_부르지_않는다(self, monkeypatch):
        calls = []
        rows = [make_row(keywords=["미생물연료전지", "Microbial Fuel Cell"])]
        out = self.run(rows, monkeypatch, calls=calls)
        assert calls == [] and [s for _, s, _ in out] == ["single_paper", "single_paper"]


class TestParsePairs:
    def test_한_키워드는_먼저_나온_쌍에만(self):
        # 실측: '도가'를 'Taoism'과 짝지은 뒤 다른 영문과 또 짝지은 경우가 있었다
        data = {"pairs": [[1, 2], [1, 3], [2, 2], [3, 1]]}
        assert pair_service._parse_pairs(data, 3, 3) == [(0, 1), (2, 0)]

    def test_범위_밖과_형식이_이상한_항목은_버린다(self):
        data = {"pairs": [[1, 9], [1], "1-1", [2, 1]]}
        assert pair_service._parse_pairs(data, 2, 2) == [(1, 0)]

    def test_없으면_빈_목록(self):
        assert pair_service._parse_pairs({"pairs": []}, 3, 3) == []


class TestParsePicks:
    def test_범위_밖과_중복_번호는_버린다(self):
        assert pair_service._parse_picks('{"picks": [2, 9, 2, "1"]}', 3) == [1, 0]

    def test_앞뒤_설명이_붙어도_JSON만_읽는다(self):
        assert pair_service._parse_picks('결과: {"picks": [3]} 입니다', 3) == [2]


class TestPaperSignature:
    def test_키워드가_바뀌면_달라진다(self):
        a = [make_row(external_id="A1", keywords=["간"])]
        b = [make_row(external_id="A1", keywords=["간", "뇌"])]
        assert pair_service._paper_signature(a) != pair_service._paper_signature(b)

    def test_논문_순서와_무관하다(self):
        x, y = make_row(external_id="A1"), make_row(external_id="A2")
        assert pair_service._paper_signature([x, y]) == pair_service._paper_signature([y, x])


class TestRoleFilter:
    def test_교신저자_필터는_공동_교신을_포함한다(self):
        role = service._author_role(person(), 3, ["co_corresponding"])
        assert service._role_matches(role, "corresponding")
        assert not service._role_matches(role, "first")

    def test_필터가_없으면_역할을_몰라도_통과(self):
        assert service._role_matches(service._author_role(person(), None, []), None)

    def test_역할을_모르면_역할_필터에_걸리지_않는다(self):
        assert not service._role_matches(service._author_role(person(), None, []), "participant")

    def test_표시_문구(self):
        role = service._author_role(person(), 1, ["first", "corresponding"])
        assert role.role_labels == ["제1저자", "교신저자"]


class TestFetchMissingRoles:
    """역할이 없는 공저 논문은 요청 중에 KCI에서 받아 저장한다."""

    class FakeDb:
        def __init__(self):
            self.saved = []
            self.committed = False

        async def execute(self, stmt, params=None):
            self.saved.extend(params or [])

        async def commit(self):
            self.committed = True

        async def rollback(self):
            pass

    def article(self, *authors):
        from app.integrations.kci.researcher_client import KCIArticle, KCIAuthor

        return SimpleNamespace(
            article=KCIArticle(
                art_id="x",
                authors=[KCIAuthor(name=n, division=d, role=p, order=i) for i, (n, d, p) in enumerate(authors, 1)],
            )
        )

    def run(self, rows, known, fetched, monkeypatch):
        calls = []

        async def fake_fetch(art_id, client=None, **_):
            calls.append(art_id)
            return fetched.get(art_id)

        monkeypatch.setattr(service, "fetch_kci_paper", fake_fetch)
        db = self.FakeDb()
        asyncio.run(service._fetch_missing_roles(db, rows, known))
        return calls, db

    def test_없는_것만_받아_저장하고_바로_쓴다(self, monkeypatch):
        rows = [make_row(external_id="ART000000001"), make_row(external_id="ART000000002")]
        known = {"ART000000001": object()}
        fetched = {"ART000000002": self.article(("김민준", "1", "제1"), ("이준호", "2", "교신"))}
        calls, db = self.run(rows, known, fetched, monkeypatch)
        assert calls == ["ART000000002"]
        assert db.committed and db.saved[0]["parts"] == ["제1", "교신"]
        assert known["ART000000002"].author_divisions == ["1", "2"]

    def test_KCI_ID가_아니면_부르지_않는다(self, monkeypatch):
        calls, db = self.run([make_row(external_id=None), make_row(external_id="REF123")], {}, {}, monkeypatch)
        assert calls == [] and db.saved == []

    def test_한_요청_상한까지만_받는다(self, monkeypatch):
        rows = [make_row(external_id=f"ART{i:09d}", pubyear=2000 + i % 20) for i in range(50)]
        calls, _ = self.run(rows, {}, {}, monkeypatch)
        assert len(calls) == service._ROLE_FETCH_MAX_PER_REQUEST

    def test_못_받으면_저장하지_않는다(self, monkeypatch):
        calls, db = self.run([make_row(external_id="ART000000003")], {}, {}, monkeypatch)
        assert calls == ["ART000000003"] and db.saved == []
