"""코퍼스 밖 논문 상세 조회의 순수 로직 테스트 (외부 API 호출 없음)."""

from types import SimpleNamespace

import pytest

from app.services.paper_citation_external_service import (
    _abstract_from_inverted,
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
