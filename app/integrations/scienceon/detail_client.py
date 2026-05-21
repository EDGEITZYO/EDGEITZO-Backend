"""ScienceON 논문 상세 API 클라이언트 (action=browse).

CitedDocumentInfo(참고문헌) 및 SimilarDocumentInfo(유사문헌)를 파싱해 반환한다.
CitingDocumentInfo는 API에서 실질적으로 제공되지 않아 수집 대상 제외.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import xmltodict

from app.core.settings import settings


@dataclass
class ReferenceItem:
    """CitedDocumentInfo — 논문이 인용한 참고문헌 1건."""
    cited_cn: str | None = None
    title: str | None = None
    author: str | None = None
    journal: str | None = None
    doi: str | None = None
    pubyear: int | None = None
    issn: str | None = None
    material_type: str | None = None


@dataclass
class SimilarItem:
    """SimilarDocumentInfo — 유사문헌 1건."""
    similar_cn: str | None = None
    title: str | None = None
    author: str | None = None
    pubyear: int | None = None
    issn: str | None = None
    material_type: str | None = None


@dataclass
class PaperDetail:
    cn: str
    references: list[ReferenceItem] = field(default_factory=list)
    similar: list[SimilarItem] = field(default_factory=list)


def _text(item: dict, code: str) -> str | None:
    """sub-item 리스트에서 metaCode 기준으로 값을 꺼낸다."""
    sub_items = item.get("item", [])
    if isinstance(sub_items, dict):
        sub_items = [sub_items]
    for sub in sub_items:
        if sub.get("@metaCode") == code:
            return sub.get("#text") or None
    return None


def _parse_year(val: str | None) -> int | None:
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


def _parse_cited(group: dict) -> ReferenceItem:
    return ReferenceItem(
        cited_cn=_text(group, "CitedCn"),
        title=_text(group, "CitedTitle"),
        author=_text(group, "CitedAuthor"),
        journal=_text(group, "CitedJournalName"),
        doi=_text(group, "CitedDOI"),
        pubyear=_parse_year(_text(group, "CitedPubyear")),
        issn=_text(group, "CitedIssn"),
        material_type=_text(group, "CitedMeterialType"),
    )


def _parse_similar(group: dict) -> SimilarItem:
    return SimilarItem(
        similar_cn=_text(group, "SimilarCn"),
        title=_text(group, "SimilarTitle"),
        author=_text(group, "SimilarAuthor"),
        pubyear=_parse_year(_text(group, "SimilarPubyear")),
        issn=_text(group, "SimilarIssn"),
        material_type=_text(group, "SimilarMeterialType"),
    )


def _parse_xml(cn: str, xml: str) -> PaperDetail:
    try:
        parsed = xmltodict.parse(xml)
    except Exception:
        return PaperDetail(cn=cn)

    try:
        status = parsed["MetaData"]["resultSummary"]["statusCode"]
        if str(status) != "200":
            return PaperDetail(cn=cn)
    except (KeyError, TypeError):
        return PaperDetail(cn=cn)

    try:
        record = parsed["MetaData"]["recordList"]["record"]
    except (KeyError, TypeError):
        return PaperDetail(cn=cn)

    items = record.get("item", [])
    if isinstance(items, dict):
        items = [items]

    references: list[ReferenceItem] = []
    similar: list[SimilarItem] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        group_code = item.get("@metaGroupCode")
        if group_code == "CitedDocumentInfo":
            references.append(_parse_cited(item))
        elif group_code == "SimilarDocumentInfo":
            similar.append(_parse_similar(item))

    return PaperDetail(cn=cn, references=references, similar=similar)


async def fetch_paper_detail(cn: str, client: httpx.AsyncClient | None = None) -> PaperDetail:
    """CN 1건의 상세 정보를 ScienceON API에서 조회한다.

    네트워크/API 오류 시 빈 PaperDetail을 반환 (호출자가 재시도 또는 skip 결정).
    """
    params = {
        "client_id": settings.scienceon_client_id,
        "token": settings.scienceon_token,
        "version": settings.scienceon_version,
        "action": "browse",
        "target": "ARTI",
        "cn": cn,
    }

    async def _call(http: httpx.AsyncClient) -> PaperDetail:
        try:
            r = await http.get(settings.scienceon_base_url, params=params)
            r.raise_for_status()
            return _parse_xml(cn, r.text)
        except Exception:
            return PaperDetail(cn=cn)

    if client is not None:
        return await _call(client)

    async with httpx.AsyncClient(timeout=20.0) as http:
        return await _call(http)
