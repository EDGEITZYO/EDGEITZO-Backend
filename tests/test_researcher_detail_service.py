from types import SimpleNamespace

import pytest

from app.services import researcher_detail_service as service


def make_row(**overrides):
    row = dict(
        internal_paper_id=None, external_id="ART000001", title="논문 제목",
        journal="한국시험학회지", pubyear=2020, pubmonth="06", pubdate=None, paper_source=None,
        authors=["홍길동", "김철수"], abstract=None, keywords=["키워드"],
        citation_count=0, db_code=None, degree=None, sci_indexed=None, doi=None,
        url="https://kci.example/ART000001", role=None, author_order=None,
    )
    row.update(overrides)
    return SimpleNamespace(**row)


class TestPublishedAt:
    """papers.pubdate에 '2022.10.30'(847건)과 '2022-10-30'(742건)이 섞여 있다."""

    def test_점구분_날짜를_대시로_통일한다(self):
        assert service._published_at(2022, "10", "2022.10.30") == "2022-10-30"

    def test_iso_날짜는_그대로_둔다(self):
        assert service._published_at(2022, "10", "2022-10-30") == "2022-10-30"

    def test_KCI_출처는_적재가_채운_일자를_버린다(self):
        # KCI API는 일자를 주지 않는데 적재가 01일을 채웠다
        assert service._published_at(2012, "05", "2012-05-01", "kci") == "2012-05"
        assert service._published_at(2012, "05", "2012.05.01", "kci_citation") == "2012-05"

    def test_원본이_준_진짜_1일은_지킨다(self):
        # ScienceON 코퍼스에는 원본에 일자까지 있는 1일 논문이 43편 있다
        assert service._published_at(2023, "03", "2023.03.01", "knowledge_base") == "2023-03-01"

    def test_일자가_없으면_연월까지만_준다(self):
        # KCI는 발행일을 주지 않는 건이 많다
        assert service._published_at(2020, "6", None) == "2020-06"

    def test_연도만_있으면_연도만_준다(self):
        assert service._published_at(2020, None, None) == "2020"

    def test_아무것도_없으면_null(self):
        assert service._published_at(None, None, None) is None


class TestToItem:
    def test_인용수_0은_0으로_보낸다(self):
        # 기존 논문 리스트와 같은 규칙 — 값이 없을 때만 null
        assert service._to_item(make_row(citation_count=0), {}, {}).citation_count == 0
        assert service._to_item(make_row(citation_count=None), {}, {}).citation_count is None

    def test_기존_논문_카드와_같은_뱃지_구조(self):
        item = service._to_item(make_row(citation_count=7), {}, {})
        assert item.trust_badge.citation_count == 7
        assert item.trust_badge.kci is item.kci_registered

    def test_인용수가_있으면_그대로_준다(self):
        assert service._to_item(make_row(citation_count=7), {}, {}).citation_count == 7

    def test_papers에_없고_KCI_ID도_없으면_북마크_불가능하다(self):
        item = service._to_item(make_row(internal_paper_id=None, external_id=None), {}, {})
        assert item.is_internal is False
        assert item.can_bookmark is False
        assert item.is_bookmarked is False

    def test_papers에_있으면_북마크와_읽음이_붙는다(self):
        row = make_row(internal_paper_id="ART000001")
        item = service._to_item(row, {"ART000001": True}, {"ART000001": "2026-09-16T00:00:00"})
        assert item.is_internal is True
        assert item.can_bookmark is True
        assert item.is_bookmarked is True
        assert item.read_at == "2026-09-16T00:00:00"

    def test_학위논문은_유형이_다르게_나온다(self):
        item = service._to_item(
            make_row(external_id=None, internal_paper_id="JAKO1", db_code="DIKO", degree="박사"), {}, {}
        )
        assert item.paper_type == "박사학위 논문"
        assert item.kci_registered is False

    def test_kci_이력_논문은_학술저널로_본다(self):
        assert service._to_item(make_row(), {}, {}).paper_type == "학술 저널"
        assert service._to_item(make_row(), {}, {}).kci_registered is True


class TestSortRows:
    def test_기본은_최신순이고_같은해는_월로_가른다(self):
        rows = [
            make_row(external_id="a", pubyear=2019, pubmonth="01"),
            make_row(external_id="b", pubyear=2021, pubmonth="03"),
            make_row(external_id="c", pubyear=2021, pubmonth="11"),
        ]
        assert [r.external_id for r in service._sort_rows(rows, "recent")] == ["c", "b", "a"]

    def test_연도가_없는_논문은_뒤로_간다(self):
        rows = [make_row(external_id="a", pubyear=None), make_row(external_id="b", pubyear=2000)]
        assert [r.external_id for r in service._sort_rows(rows, "recent")] == ["b", "a"]

    def test_피인용순은_미집계를_맨_뒤로_보낸다(self):
        rows = [
            make_row(external_id="a", citation_count=None),
            make_row(external_id="b", citation_count=5),
            make_row(external_id="c", citation_count=0),
        ]
        # None은 '모름'이라 0보다도 뒤에 둔다
        assert [r.external_id for r in service._sort_rows(rows, "citations")] == ["b", "c", "a"]


class TestEmailVisibility:
    """남의 연락처라 근거가 확실한 등급만 내보낸다 (명세 08-01)."""

    class FakeResult:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class FakeDb:
        def __init__(self, row):
            self._row = row

        async def execute(self, *_args, **_kwargs):
            return TestEmailVisibility.FakeResult(self._row)

    @staticmethod
    def profile_row(confidence):
        return SimpleNamespace(
            researcher_id="kci:x", source="kci", author_name_kor="홍길동", author_name_eng=None,
            institution_current="가나대학교", author_inst_kor=None, institution_dept=None,
            keywords=["키워드"], email="someone@example.ac.kr", match_confidence=confidence,
            total_papers=10, article_cnt=None, total_citations=3, citation_source="kci",
            corpus_paper_count=1, first_pubyear=2010, last_pubyear=2020,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("confidence", ["confirmed", "domain_verified"])
    async def test_근거가_확실하면_노출한다(self, confidence):
        db = self.FakeDb(self.profile_row(confidence))
        profile = await service.get_profile(db, "kci:x")
        assert profile.email == "someone@example.ac.kr"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("confidence", ["inferred", None])
    async def test_추정이거나_출처_미상이면_가린다(self, confidence):
        # 동명이인일 때 엉뚱한 사람의 주소가 프로필에 뜨는 것을 막는다
        db = self.FakeDb(self.profile_row(confidence))
        profile = await service.get_profile(db, "kci:x")
        assert profile.email is None

    @pytest.mark.asyncio
    async def test_없는_연구자는_None을_돌려준다(self):
        assert await service.get_profile(self.FakeDb(None), "kci:없음") is None


class _FakeDB:
    """researchers / researcher_id_aliases 두 표만 흉내 낸다."""

    def __init__(self, researchers, aliases):
        self.researchers, self.aliases = set(researchers), dict(aliases)

    async def execute(self, _sql, params):
        rid = params["rid"]
        hit = rid if rid in self.researchers else self.aliases.get(rid)
        row = SimpleNamespace(researcher_id=hit) if hit else None
        return SimpleNamespace(first=lambda: row)


class TestResolveResearcherId:
    """중복 ID를 합친 뒤에도 옛 링크가 404가 되지 않아야 한다 (037)."""

    @pytest.mark.asyncio
    async def test_있는_ID는_그대로(self):
        db = _FakeDB({"kci:a"}, {})
        assert await service.resolve_researcher_id(db, "kci:a") == "kci:a"

    @pytest.mark.asyncio
    async def test_합쳐진_옛_ID는_남은_ID로(self):
        db = _FakeDB({"kci:a"}, {"kci:old": "kci:a"})
        assert await service.resolve_researcher_id(db, "kci:old") == "kci:a"

    @pytest.mark.asyncio
    async def test_없는_ID는_None(self):
        assert await service.resolve_researcher_id(_FakeDB(set(), {}), "kci:x") is None


class TestDepartmentDisplay:
    """전공이 없으면 소속으로 대체한다 — 원래 department 값은 그대로 둔다."""

    def test_전공이_있으면_전공(self):
        assert service._department_display("환경공학과", "충북대학교") == {
            "department_display": "환경공학과", "department_source": "department"}

    def test_전공이_없으면_소속(self):
        assert service._department_display(None, "충북대학교") == {
            "department_display": "충북대학교", "department_source": "institution"}

    def test_둘_다_없으면_null(self):
        assert service._department_display(" ", None) == {"department_display": None, "department_source": None}


class TestDetailId:
    """papers에 행이 없어도 KCI ID면 상세·북마크 API가 그 자리에서 적재한다."""

    def test_우리_DB_논문이면_그_ID(self):
        item = service._to_item(make_row(internal_paper_id="JAKO1", external_id="ART1"), {}, {})
        assert item.detail_id == "JAKO1" and item.can_open_detail and item.can_bookmark

    def test_DB에_없어도_KCI_ID면_상세로_갈_수_있다(self):
        item = service._to_item(make_row(internal_paper_id=None, external_id="ART002780520"), {}, {})
        assert item.detail_id == "ART002780520" and item.can_open_detail and item.can_bookmark
        assert item.is_internal is False

    def test_둘_다_없으면_막는다(self):
        item = service._to_item(make_row(internal_paper_id=None, external_id=None), {}, {})
        assert item.detail_id is None and not item.can_open_detail and not item.can_bookmark


class TestFilters:
    """키워드맵 논문 목록과 같은 이름·의미의 필터."""

    rows = [
        make_row(external_id="ART1", pubyear=2020, sci_indexed=True, db_code=None),
        make_row(external_id="ART2", pubyear=2021, sci_indexed=False, db_code=None),
        make_row(external_id=None, internal_paper_id="DIKO1", pubyear=2021, db_code="DIKO", degree="박사", sci_indexed=None),
    ]

    def test_연도는_그_해만(self):
        assert [r.external_id for r in service.apply_filters(self.rows, year=2020)] == ["ART1"]

    def test_논문_유형(self):
        assert len(service.apply_filters(self.rows, paper_type="박사학위 논문")) == 1
        assert len(service.apply_filters(self.rows, paper_type="전체")) == 3

    def test_KCI_SCI(self):
        assert len(service.apply_filters(self.rows, kci=True)) == 2
        assert [r.external_id for r in service.apply_filters(self.rows, sci=True)] == ["ART1"]
