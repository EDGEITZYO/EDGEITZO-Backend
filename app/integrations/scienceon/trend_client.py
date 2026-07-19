"""ScienceON TREND API 클라이언트 (target=TREND).

키워드 정의(Definition)/출처 URL(DefinitionSourceURL)을 제공하는 유일한 ScienceON 엔드포인트.
연구자 API와 달리 배치 적재 대상이 아니라 상세 패널 요청 시점에 라이브로 호출한다
(키워드 1개당 가벼운 단건 조회라 미리 적재해둘 필요가 없음).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import xmltodict

from app.core.settings import settings


@dataclass
class TrendItem:
    cn: str | None = None
    title: str | None = None
    related_keywords: str | None = None
    definition: str | None = None
    definition_source_url: str | None = None
    thumbnail_url: str | None = None
    publ_date: str | None = None
    content_url: str | None = None
    pdf_url: str | None = None


class ScienceOnTrendClient:
    def __init__(self):
        self.base_url = settings.scienceon_base_url
        self.client_id = settings.scienceon_client_id
        self.token = settings.scienceon_token
        self.version = settings.scienceon_version

    def _build_common_params(self) -> dict:
        return {
            "client_id": self.client_id,
            "token": self.token,
            "version": self.version,
        }

    async def search_trends(
        self,
        query: str,
        *,
        search_field: str = "TI",  # 'BI'(전체) | 'TI'(Trend명) | 'KW'(키워드 클라우드) | 'DL'(생성일)
        page: int = 1,
        size: int = 10,
        sort_field: str | None = None,  # 'newdoc' | 'title' | None(최신자료순)
        sort_by: str | None = None,  # 'asc' | 'desc'
    ) -> str:
        params = self._build_common_params()
        params.update(
            {
                "action": "search",
                "target": "TREND",
                "searchQuery": json.dumps({search_field: query}, ensure_ascii=False),
                "curPage": page,
                "rowCount": size,
            }
        )
        if sort_field:
            params["sortField"] = sort_field
        if sort_by:
            params["sortBy"] = sort_by

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(self.base_url, params=params)
            response.raise_for_status()
            return response.text


def _text(item: dict, code: str) -> str | None:
    """sub-item 리스트에서 metaCode 기준으로 값을 꺼낸다 (detail_client.py의 _text와 동일 패턴)."""
    sub_items = item.get("item", [])
    if isinstance(sub_items, dict):
        sub_items = [sub_items]
    for sub in sub_items:
        if sub.get("@metaCode") == code:
            return sub.get("#text") or None
    return None


def _parse_trend_record(record: dict) -> TrendItem:
    return TrendItem(
        cn=_text(record, "CN"),
        title=_text(record, "Title"),
        related_keywords=_text(record, "RelatedKeywords"),
        definition=_text(record, "Definition"),
        definition_source_url=_text(record, "DefinitionSourceURL"),
        thumbnail_url=_text(record, "ThumbnailURL"),
        publ_date=_text(record, "PublDate"),
        content_url=_text(record, "ContentURL"),
        pdf_url=_text(record, "PdfURL"),
    )


def parse_trend_search_xml(xml: str) -> list[TrendItem]:
    try:
        parsed = xmltodict.parse(xml)
    except Exception:
        return []

    try:
        status = parsed["MetaData"]["resultSummary"]["statusCode"]
        if str(status) != "200":
            return []
    except (KeyError, TypeError):
        return []

    try:
        records = parsed["MetaData"]["recordList"]["record"]
    except (KeyError, TypeError):
        return []

    if isinstance(records, dict):
        records = [records]

    return [_parse_trend_record(r) for r in records if isinstance(r, dict)]
