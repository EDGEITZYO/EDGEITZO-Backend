"""국내(KCI) 논문 판정·실시간 적재 흐름 테스트 (외부 API·DB 호출 없음)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import domestic_paper_service as svc
from app.services.paper_citation_service import _card_from_external, _node_from_external, _refs_pending

_XML = """<?xml version="1.0" encoding="UTF-8"?>
<MetaData><outputData><record>
  <journalInfo journal-id="000900">
    <issn>1226-8763</issn>
    <journal-name>원예과학기술지</journal-name>
    <publisher-name>한국원예학회</publisher-name>
    <kci-registration>등재</kci-registration>
    <pub-year>2017</pub-year><pub-mon>12</pub-mon>
  </journalInfo>
  <articleInfo article-id="ART002295537">
    <title-group>
      <article-title lang="original"><![CDATA[저온 처리 배추의 항산화 효소]]></article-title>
      <article-title lang="english"><![CDATA[Antioxidant Enzymes in Kimchi Cabbage]]></article-title>
    </title-group>
    <author-group><author><name>이희주</name></author></author-group>
    <abstract-group><abstract lang="original">배추 잎의 항산화 효소 활성을 조사하였다.</abstract></abstract-group>
    <keyword-group><keyword>배추</keyword><keyword>Antioxidant</keyword></keyword-group>
    <doi>http://dx.doi.org/10.12925/jkocs.2013.30.3.371</doi>
  </articleInfo>
  <referenceInfo>
    <reference arti-id="ART001234567" refebibl-id="REF000000001">
      <title>국내 선행 연구</title><author>김철수; 이영희</author><pubi-year>2010</pubi-year>
    </reference>
    <reference refebibl-id="REF045167936">
      <title>Impacts of chilling temperatures</title><author>Damian J. Allen</author>
      <journal-name>Trends in Plant Science</journal-name><pubi-year>2001</pubi-year>
      <doi><![CDATA[http://dx.doi.org/10.1016/S1360-1385(00)01808-2]]></doi>
    </reference>
    <reference><title>ID 없는 항목</title></reference>
  </referenceInfo>
</record></outputData></MetaData>"""


@pytest.mark.parametrize(
    "key,expected",
    [
        ("ART002295537", True),
        ("REF045167936", False),
        ("W4401234567", False),
        ("JAKO202509339655899", False),  # 코퍼스 논문 ID — KCI ID로 판정하지 않는다
        ("NART125449884", False),
        ("ART", False),
        ("", False),
        (None, False),
    ],
)
def test_is_domestic_key(key, expected):
    assert svc.is_domestic_key(key) is expected


def test_parse_kci_paper_reads_detail_and_references():
    paper = svc.parse_kci_paper(_XML)
    assert paper is not None
    assert paper.article.art_id == "ART002295537"
    assert paper.article.title == "저온 처리 배추의 항산화 효소"
    assert paper.publisher == "한국원예학회"

    # ID 없는 참고문헌은 노드로 쓸 수 없어 버린다
    assert [r.external_id for r in paper.references] == ["ART001234567", "REF045167936"]
    domestic, foreign = paper.references
    assert domestic.arti_id == "ART001234567"
    assert domestic.authors == ["김철수", "이영희"]
    assert foreign.arti_id is None
    assert foreign.pubyear == 2001
    assert foreign.journal == "Trends in Plant Science"


@pytest.mark.parametrize(
    "raw,expected",
    [("2001", 2001), ("197519751984", 1975), ("2019.3", 2019), ("미상", None), ("", None), (None, None)],
)
def test_reference_year_takes_first_plausible_year(raw, expected):
    assert svc._year_or_none(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1016/abc", "https://doi.org/10.1016/abc"),
        ("http://dx.doi.org/10.1016/abc", "https://doi.org/10.1016/abc"),
        ("https://doi.org/10.1016/abc", "https://doi.org/10.1016/abc"),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize_doi(raw, expected):
    assert svc._normalize_doi(raw) == expected


def test_doi_forms_cover_bare_and_url_variants():
    forms = svc._doi_forms("https://doi.org/10.1/x")
    assert "10.1/x" in forms and "https://doi.org/10.1/x" in forms and "http://dx.doi.org/10.1/x" in forms


def _ref(external_id, **kw):
    base = dict(
        external_id=external_id, title="t", title_en=None, authors=["a"], journal="j", pubyear=2020,
        doi=None, resolved_doi=None, abstract=None, keywords=None, citation_count=None, kci_registered=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_external_art_node_is_domestic_and_expandable_on_references():
    node = _node_from_external(_ref("ART001234567"), tier=1, side="child", direction="reference")
    assert node.in_service is True
    assert node.paper_id == "ART001234567"
    assert node.has_more is True
    # 피인용은 KCI 응답에 없어 받아 와도 늘지 않는다
    assert _node_from_external(_ref("ART001234567"), tier=1, side="child", direction="citing").has_more is False


@pytest.mark.parametrize("key", ["REF045167936", "W4401234567"])
def test_external_non_kci_node_is_foreign(key):
    node = _node_from_external(_ref(key), tier=1, side="child", direction="reference")
    assert node.in_service is False
    assert node.paper_id is None
    assert node.has_more is False


def test_domestic_ref_card_uses_enriched_fields():
    card = _card_from_external(
        _ref("ART001234567", abstract="초록", keywords=["배추"], citation_count=3, kci_registered=True)
    )
    assert card.in_service is True
    assert card.paper_id == "ART001234567"
    assert card.abstract == "초록"
    assert card.keywords == ["배추"]
    assert card.trust_badge.kci is True
    assert card.is_bookmarked is False


def test_foreign_card_keeps_bibliography_only():
    card = _card_from_external(_ref("REF045167936", abstract="should not leak"))
    assert card.in_service is False
    assert card.abstract is None
    assert card.trust_badge is None


@pytest.mark.asyncio
async def test_refs_pending_uses_this_environments_postgres():
    """papers 행이 없거나 kci_refs_loaded_at이 NULL인 국내 논문만 대기 중. 코퍼스 CN·해외 key는 대상 아님."""
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ["ART001"]))
    pending = await _refs_pending(db, ["ART001", "ART002", "JAKO2025", "REF045167936"])
    assert pending == {"ART002"}


@pytest.mark.asyncio
async def test_refs_pending_skips_query_without_domestic_keys():
    db = AsyncMock()
    assert await _refs_pending(db, ["JAKO2025", "REF1"]) == set()
    db.execute.assert_not_called()


# ---------------------------------------------------------------------------
# materialize_domestic_paper 흐름 (DB/Neo4j/KCI는 모두 대체)
# ---------------------------------------------------------------------------

@pytest.fixture
def patched(monkeypatch):
    mocks = SimpleNamespace(
        resolve=AsyncMock(return_value={}),
        fetch=AsyncMock(return_value=None),
        link=AsyncMock(),
        insert=AsyncMock(),
        node_exists=False,
    )
    monkeypatch.setattr(svc, "resolve_papers", mocks.resolve)
    monkeypatch.setattr(svc, "fetch_kci_paper", mocks.fetch)
    monkeypatch.setattr(svc, "_link", mocks.link)
    monkeypatch.setattr(svc, "_insert_paper", mocks.insert)
    monkeypatch.setattr(svc, "_neo4j_node_exists", lambda cn: mocks.node_exists)
    return mocks


def _db(same_doi_row=None):
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar=lambda: None, first=lambda: same_doi_row)
    return db


@pytest.mark.asyncio
async def test_materialize_returns_none_for_unknown_foreign_key(patched):
    assert await svc.materialize_domestic_paper(_db(), "REF045167936") is None
    patched.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_skips_everything_when_already_linked(patched):
    patched.resolve.return_value = {"ART001": {"id": "ART001", "kci_art_id": "ART001", "kci_refs_loaded_at": "2026-09-19"}}
    patched.node_exists = True
    assert await svc.materialize_domestic_paper(_db(), "ART001") == "ART001"
    patched.fetch.assert_not_called()
    patched.link.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_corpus_paper_without_node_links_without_kci_call(patched):
    """ScienceON CN 코퍼스 논문은 참고문헌이 이미 적재돼 있어 KCI를 부르지 않는다."""
    patched.resolve.return_value = {"JAKO1": {"id": "JAKO1", "kci_art_id": None, "kci_refs_loaded_at": None}}
    patched.node_exists = False
    assert await svc.materialize_domestic_paper(_db(), "JAKO1") == "JAKO1"
    patched.fetch.assert_not_called()
    patched.link.assert_awaited_once()
    assert patched.link.await_args.kwargs["mark_loaded"] is False


@pytest.mark.asyncio
async def test_materialize_new_kci_paper_inserts_then_links_references(patched):
    fetched = svc.parse_kci_paper(_XML)
    patched.fetch.return_value = fetched
    row = {"id": "ART002295537", "kci_art_id": "ART002295537"}
    patched.resolve.side_effect = [{}, {"ART002295537": row}]

    assert await svc.materialize_domestic_paper(_db(), "ART002295537") == "ART002295537"
    patched.insert.assert_awaited_once()
    patched.fetch.assert_awaited_once()  # 상세·참고문헌을 한 번의 호출로
    args = patched.link.await_args
    assert args.args[2] == fetched.references
    assert args.kwargs["mark_loaded"] is True


@pytest.mark.asyncio
async def test_materialize_returns_none_when_kci_has_no_such_paper(patched):
    assert await svc.materialize_domestic_paper(_db(), "ART999999999") is None
    patched.insert.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_does_not_merge_different_paper_sharing_issue_doi(patched):
    """KCI가 호(issue) 단위 DOI를 여러 논문에 똑같이 붙여 온다. 제목이 다르면 다른 논문이다 —
    합치면 상세페이지에 엉뚱한 논문이 뜨고 참고문헌이 그 논문에 붙는다(실제 발생)."""
    fetched = svc.parse_kci_paper(_XML)
    patched.fetch.return_value = fetched
    row = {"id": "ART002295537", "kci_art_id": "ART002295537"}
    patched.resolve.side_effect = [{}, {"ART002295537": row}]
    other = SimpleNamespace(id="ART001866021", title="혹서기 무창계사 육계의 혈액지질", title_en=None)

    assert await svc.materialize_domestic_paper(_db(same_doi_row=other), "ART002295537") == "ART002295537"
    patched.insert.assert_awaited_once()
    assert patched.insert.await_args.kwargs["keep_doi"] is False


@pytest.mark.asyncio
async def test_materialize_reuses_same_paper_with_same_doi_and_title(patched):
    fetched = svc.parse_kci_paper(_XML)
    patched.fetch.return_value = fetched
    existing = {"id": "JAKO2017", "kci_art_id": None, "kci_refs_loaded_at": None}
    patched.resolve.side_effect = [{}, {"JAKO2017": existing}]
    patched.node_exists = True
    same = SimpleNamespace(id="JAKO2017", title="저온 처리 배추의 항산화 효소", title_en=None)

    assert await svc.materialize_domestic_paper(_db(same_doi_row=same), "ART002295537") == "JAKO2017"
    patched.insert.assert_not_called()
    # 요청 key(ART…)를 가리키던 참고문헌 행도 이 논문으로 옮긴다
    assert patched.link.await_args.kwargs["aliases"] == {"ART002295537"}


# ---------------------------------------------------------------------------
# 그래프 카드: 노드와 카드는 항상 1:1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cards_cover_unloaded_domestic_and_missing_rows(monkeypatch):
    from app.services import paper_citation_service as pcs

    async def fake_in_service_cards(db, cns):
        return {}  # 어떤 논문도 이 환경 Postgres에 없다

    monkeypatch.setattr(pcs, "_build_in_service_cards", fake_in_service_cards)
    unloaded = _ref("ART001234567", abstract="초록")
    foreign = _ref("REF045167936")
    nodes = [
        _node_from_external(unloaded, tier=1, side="child", direction="reference"),
        _node_from_external(foreign, tier=1, side="child", direction="reference"),
        # Neo4j에만 있는 국내 논문 노드
        pcs.PaperCitationNode(key="ART009999999", in_service=True, paper_id="ART009999999",
                              title="그래프에만 있는 논문", pubyear=2020, tier=1, side="child"),
    ]
    cards = await pcs._build_cards_for_nodes(None, nodes, {r.external_id: r for r in (unloaded, foreign)})

    assert [c.key for c in cards] == [n.key for n in nodes]
    assert cards[0].in_service is True and cards[0].abstract == "초록"
    assert cards[1].in_service is False
    assert cards[2].in_service is True and cards[2].title == "그래프에만 있는 논문"


@pytest.mark.asyncio
async def test_materialize_loads_refs_when_other_environment_already_linked_graph(patched):
    """공유 Neo4j에는 노드·CITES가 이미 있어도(다른 환경이 적재) 이 환경 Postgres에 참고문헌 행이
    없으면(kci_refs_loaded_at NULL) KCI에서 받아 넣어야 한다 — 안 그러면 해외 참고문헌이 그래프에서 빠진다."""
    fetched = svc.parse_kci_paper(_XML)
    patched.fetch.return_value = fetched
    patched.resolve.return_value = {"ART002295537": {"id": "ART002295537", "kci_art_id": "ART002295537", "kci_refs_loaded_at": None}}
    patched.node_exists = True

    assert await svc.materialize_domestic_paper(_db(), "ART002295537") == "ART002295537"
    patched.fetch.assert_awaited_once()
    assert patched.link.await_args.kwargs["mark_loaded"] is True


@pytest.mark.asyncio
async def test_cards_show_real_bookmark_state_for_logged_in_user(monkeypatch):
    """예전엔 인용관계 그래프 카드의 is_bookmarked가 항상 false였다."""
    import uuid as _uuid
    from app.services import paper_citation_service as pcs

    async def fake_in_service_cards(db, cns):
        return {}

    async def fake_bookmarked(db, user_id, ids):
        return {"ART009999999"}

    monkeypatch.setattr(pcs, "_build_in_service_cards", fake_in_service_cards)
    monkeypatch.setattr(pcs, "get_bookmarked_paper_ids", fake_bookmarked)
    foreign = _ref("REF045167936")
    nodes = [
        pcs.PaperCitationNode(key="ART009999999", in_service=True, paper_id="ART009999999", title="a", tier=1, side="child"),
        pcs.PaperCitationNode(key="ART008888888", in_service=True, paper_id="ART008888888", title="b", tier=1, side="child"),
        _node_from_external(foreign, tier=1, side="child", direction="reference"),
    ]
    cards = await pcs._build_cards_for_nodes(None, nodes, {foreign.external_id: foreign}, _uuid.uuid4())
    assert [c.is_bookmarked for c in cards] == [True, False, None]
