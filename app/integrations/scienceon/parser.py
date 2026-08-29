from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import xmltodict


def parse_scienceon_xml(xml_text: str) -> dict[str, Any]:
    parsed = xmltodict.parse(xml_text)
    return parsed


class ScienceOnApiError(RuntimeError):
    """ScienceON API가 200 이외의 statusCode를 반환하거나 응답을 해석할 수 없는 경우.

    토큰 만료(401/E4103)·호출 한도 초과 등은 XML 자체는 멀쩡해서 그냥 파싱하면
    "참고문헌 0건"과 구분되지 않는다. 장애를 빈 결과로 삼키지 않도록 예외로 올린다.
    """

    def __init__(self, status_code: str | None, error_code: str | None = None, message: str | None = None):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(f"ScienceON API 오류 (statusCode={status_code}, errorCode={error_code}): {message}")


@dataclass
class ScienceOnReference:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None


def _element_text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    return (el.text or "").strip() if el is not None and el.text else None


def parse_cited_references(xml_text: str) -> list[ScienceOnReference]:
    """browse 응답 XML에서 CitedDocumentInfo(참고문헌) 파싱.

    ScienceON이 오류를 반환하면 ScienceOnApiError를 올린다 (빈 리스트로 삼키지 않음).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ScienceOnApiError(status_code=None, message=f"응답 XML 파싱 실패: {exc}") from exc

    status_code = _element_text(root, "./resultSummary/statusCode")
    if status_code is not None and status_code != "200":
        raise ScienceOnApiError(
            status_code=status_code,
            error_code=_element_text(root, "./errorDetail/errorCode"),
            message=_element_text(root, "./errorDetail/errorMessage"),
        )

    refs: list[ScienceOnReference] = []

    for group in root.findall(".//*[@metaGroupCode='CitedDocumentInfo']"):
        def _get(code: str) -> str:
            el = group.find(f"item[@metaCode='{code}']")
            return (el.text or "").strip() if el is not None else ""

        title = _get("CitedTitle") or None

        raw_authors = _get("CitedAuthor")
        authors = [a.strip() for a in raw_authors.split(";") if a.strip()]

        year_raw = _get("CitedPubyear")
        try:
            year = int(year_raw) if year_raw else None
        except ValueError:
            year = None

        journal = _get("CitedJournalName") or None

        raw_doi = _get("CitedDOI")
        doi: str | None = None
        if raw_doi:
            for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
                if raw_doi.startswith(prefix):
                    raw_doi = raw_doi[len(prefix):]
                    break
            doi = raw_doi if raw_doi.startswith("10.") else None

        # CitedCn은 인용된 논문의 CN이 아니라 "<원문id>_<순번>" 형태의 참고문헌 항목 일련번호라
        # papers.id와 매칭할 수 없다. in_service 판별은 CitedDOI만 사용한다.
        refs.append(ScienceOnReference(title=title, authors=authors, year=year, journal=journal, doi=doi))

    return refs