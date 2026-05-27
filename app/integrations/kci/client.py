"""KCI Open API — articleDetail 기반 참고문헌 조회."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"
_API_KEY = "64625154"


@dataclass
class KCIReference:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    unstructured: str | None = None


def _parse_references(xml_text: str) -> list[KCIReference]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("KCI XML 파싱 실패: %s", exc)
        return []

    refs: list[KCIReference] = []
    for ref in root.iter("reference"):
        title = (ref.findtext("title") or "").strip() or None
        journal = (ref.findtext("journal-name") or "").strip() or None
        doi = (ref.findtext("doi") or "").strip() or None

        year_raw = (ref.findtext("pubi-year") or "").strip()
        try:
            year = int(year_raw) if year_raw else None
        except ValueError:
            year = None

        # author 태그 내 세미콜론 분리 처리 (여러 명이 한 태그에 묶인 경우 포함)
        raw_authors: list[str] = [
            a.text.strip()
            for a in ref.findall("author")
            if a.text and a.text.strip()
        ]
        authors: list[str] = []
        for raw in raw_authors:
            for name in raw.split(";"):
                name = name.strip()
                if name:
                    authors.append(name)

        # refebibl-id를 unstructured fallback으로 사용
        ref_id = (ref.findtext("refebibl-id") or "").strip() or None

        refs.append(KCIReference(
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            doi=doi if doi and doi.startswith("10.") else None,
            unstructured=ref_id,
        ))

    return refs


async def get_references(art_id: str) -> list[KCIReference]:
    """KCI articleDetail API로 참고문헌 목록 반환. 실패 시 빈 리스트."""
    params = {
        "apiCode": "articleDetail",
        "key": _API_KEY,
        "id": art_id,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_BASE_URL, params=params)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("KCI articleDetail 조회 실패 art_id=%s: %s", art_id, exc)
        return []

    return _parse_references(resp.text)
