from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import xmltodict


def parse_scienceon_xml(xml_text: str) -> dict[str, Any]:
    parsed = xmltodict.parse(xml_text)
    return parsed


@dataclass
class ScienceOnReference:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None


def parse_cited_references(xml_text: str) -> list[ScienceOnReference]:
    """browse 응답 XML에서 CitedDocumentInfo(참고문헌) 파싱."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

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

        refs.append(ScienceOnReference(title=title, authors=authors, year=year, journal=journal, doi=doi))

    return refs