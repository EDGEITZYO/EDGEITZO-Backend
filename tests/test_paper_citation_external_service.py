"""코퍼스 밖 논문 상세 조회의 순수 로직 테스트 (외부 API 호출 없음)."""

from types import SimpleNamespace

import pytest

from app.services.paper_citation_external_service import (
    _ENRICHED_FIELDS,
    _abstract_from_inverted,
    _detail_from_stored,
    _is_korean,
    _merge_stored,
    _normalize_doi,
    _openalex_id_of,
)


def test_abstract_from_inverted_restores_word_order():
    inverted = {"climate": [2], "Global": [0], "is": [3], "change": [1], "real": [4]}
    assert _abstract_from_inverted(inverted) == "Global change climate is real"


def test_abstract_from_inverted_handles_repeated_words():
    assert _abstract_from_inverted({"the": [0, 2], "end": [1, 3]}) == "the end the end"


@pytest.mark.parametrize("value", [None, {}])
def test_abstract_from_inverted_empty(value):
    assert _abstract_from_inverted(value) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://doi.org/10.1038/NATURE12373", "10.1038/nature12373"),
        ("http://dx.doi.org/10.1234/AbC", "10.1234/abc"),
        ("10.1234/abc", "10.1234/abc"),
        ("  ", None),
        (None, None),
    ],
)
def test_normalize_doi(raw, expected):
    assert _normalize_doi(raw) == expected


def test_is_korean_needs_more_than_a_few_hangul():
    """제목에 한글이 한두 자 섞인 영문 초록을 한국어로 오판하면 안 된다."""
    assert _is_korean("한" * 21)
    assert not _is_korean("Mostly English with 한글 세 글자")


def test_openalex_id_of_extracts_short_id():
    assert _openalex_id_of({"id": "https://openalex.org/W4391226981"}) == "W4391226981"
    assert _openalex_id_of({}) is None


def test_merge_stored_fills_each_field_from_first_row_that_has_it():
    """같은 논문이 여러 source_cn 아래에 들어가 있고 행마다 빠진 필드가 달라, 필드 단위로 합쳐야
    가장 완전한 서지정보가 나온다."""
    rows = [
        SimpleNamespace(title="제목", authors=None, journal=None, doi=None, pubyear=None, external_source="kci"),
        SimpleNamespace(title="다른 제목", authors=["저자"], journal="학술지", doi="10.1/x", pubyear=2020, external_source="kci"),
    ]
    merged = _merge_stored(rows)
    assert merged["title"] == "제목"  # 먼저 채워진 값을 유지
    assert merged["authors"] == ["저자"]
    assert merged["journal"] == "학술지"
    assert merged["doi"] == "10.1/x"
    assert merged["pubyear"] == 2020


def test_merge_stored_all_empty():
    rows = [SimpleNamespace(title=None, authors=None, journal=None, doi=None, pubyear=None, external_source=None)]
    assert all(v is None for v in _merge_stored(rows).values())


# ---------------------------------------------------------------------------
# 사전 적재된 값으로 상세를 만드는 경로 (외부 호출 없음)
# ---------------------------------------------------------------------------

def _stored(**overrides):
    base = {
        "title": "Stress-induced rearrangement of Fusarium retrotransposon sequences",
        "authors": ["Anaya N"], "journal": "Mol Genl Genet", "pubyear": 1996,
        "doi": None, "external_source": "kci",
    }
    base.update({field: None for field in _ENRICHED_FIELDS})
    base.update(overrides)
    return base


def test_detail_from_stored_uses_resolved_doi_over_stored_doi():
    """Crossref로 찾아낸 DOI가 있으면 그쪽을 쓴다 — KCI가 준 doi는 비어 있는 경우가 대부분이고,
    둘 다 있으면 실제로 조회에 성공한 resolved_doi가 더 신뢰할 만하다."""
    detail = _detail_from_stored("REF025243630", _stored(
        doi="10.1111/old", resolved_doi="10.1007/BF00279737", enrich_status="ok", abstract="x" * 50,
    ))
    assert detail.doi == "10.1007/bf00279737"


def test_detail_from_stored_falls_back_to_doi_org_link():
    """external_url이 없어도 DOI가 있으면 최소한 논문에 도달할 링크는 준다."""
    detail = _detail_from_stored("REF1", _stored(resolved_doi="10.1234/abc", enrich_status="no_abstract"))
    assert detail.external_url == "https://doi.org/10.1234/abc"


def test_detail_from_stored_marks_tldr_source_distinctly():
    """tldr은 사람이 쓴 초록이 아니라 AllenAI 생성 요약이다. 프런트가 다르게 표기할 수 있도록
    enrich_source로 구분돼야 한다 — 값이 abstract 필드에 담긴다는 이유로 뭉뚱그리면 안 된다."""
    detail = _detail_from_stored("REF2", _stored(
        abstract="One-line summary.", abstract_source="s2_tldr", enrich_status="ok",
    ))
    assert detail.enrich_source == "s2_tldr"
    assert detail.enriched is True


def test_detail_from_stored_not_enriched_when_no_abstract():
    """조회는 끝났지만(enrich_status 있음) 초록을 못 찾은 경우, enriched=false로 내려가
    프런트가 초록 영역을 비우거나 안내 문구로 대체할 수 있어야 한다. 404가 아니다."""
    detail = _detail_from_stored("REF3", _stored(enrich_status="no_match"))
    assert detail.enriched is False
    assert detail.abstract is None
    assert detail.title  # 서지정보는 그대로 남는다


def test_detail_from_stored_copies_list_fields_defensively():
    """authors/keywords는 SQLAlchemy가 돌려준 리스트를 그대로 참조하면 안 된다(세션 밖에서 변형 위험)."""
    keywords = ["a", "b"]
    detail = _detail_from_stored("REF4", _stored(keywords=keywords, enrich_status="ok", abstract="x"))
    assert detail.keywords == ["a", "b"]
    assert detail.keywords is not keywords
