from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.crossref.org/works"
_USER_AGENT = f"EDGEITZO/1.0 (mailto:{settings.crossref_contact_email})"


@dataclass
class Reference:
    doi: str | None = None
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    in_service: bool = False
    unstructured: str | None = None


def _parse_authors(raw: object) -> list[str]:
    """CrossRef author 필드 파싱. 문자열이면 세미콜론 분리, 리스트면 family/given 조합."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [a.strip() for a in raw.split(";") if a.strip()]
    if isinstance(raw, list):
        result = []
        for a in raw:
            if isinstance(a, dict):
                name = ", ".join(filter(None, [a.get("family"), a.get("given")]))
                if name:
                    result.append(name)
        return result
    return []


async def get_references(doi: str) -> list[Reference]:
    """DOI로 CrossRef API를 조회해 참고문헌 목록을 반환. 실패 시 빈 리스트."""
    from app.repositories.paper_repository import normalize_doi
    url = f"{_BASE_URL}/{normalize_doi(doi)}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("CrossRef 조회 실패 doi=%s: %s", doi, exc)
        return []

    raw_refs = data.get("message", {}).get("reference", [])
    refs: list[Reference] = []
    for r in raw_refs:
        year_raw = r.get("year")
        try:
            year = int(year_raw) if year_raw else None
        except (ValueError, TypeError):
            year = None

        refs.append(Reference(
            doi=r.get("DOI") or None,
            title=r.get("article-title") or r.get("volume-title") or None,
            authors=_parse_authors(r.get("author")),
            year=year,
            journal=r.get("journal-title") or None,
            unstructured=r.get("unstructured") or None,
        ))
    return refs
