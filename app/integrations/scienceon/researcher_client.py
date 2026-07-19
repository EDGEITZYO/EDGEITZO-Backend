"""ScienceON 연구자 검색/상세보기 API 클라이언트 (target=RESEARCHER).

요청/응답 파싱까지 완전히 구현되어 있으며, 실제 배치 적재 스크립트만 별도(미작성) —
데이터가 없어도 get_related_researchers()가 빈 리스트를 반환하도록 서비스 계층에서 처리한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import xmltodict

from app.core.settings import settings


@dataclass
class ResearcherRecord:
    cn: str | None = None
    author_name_kor: str | None = None
    author_name_eng: str | None = None
    author_inst_kor: str | None = None
    author_inst_eng: str | None = None
    keyword: str | None = None
    rno: str | None = None
    email: str | None = None
    article_cnt: int | None = None
    patent_cnt: int | None = None
    report_cnt: int | None = None


@dataclass
class CallAPIInfo:
    """연구자 상세보기 응답에 포함된, 그 연구자의 논문/특허/보고서 목록을 가져오는 방법 안내.
    실제 논문 목록은 여기 담긴 파라미터로 기존 논문 검색 API를 별도 호출해서 얻는다."""
    provider_api_id: str | None = None
    provider_api_name: str | None = None
    parameter_values: dict = field(default_factory=dict)


class ScienceOnResearcherClient:
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

    async def search_researchers(
        self,
        query: str,
        *,
        search_field: str = "TI",  # 'BI'(전체) | 'TI'(연구자명) | 'CN'(저자일련번호)
        page: int = 1,
        size: int = 10,
        sort_field: str | None = None,  # 'newdoc' | 'kor_autnm' | 'eng_autnm' | None(정확도)
        sort_by: str | None = None,  # 'asc' | 'desc'
    ) -> str:
        params = self._build_common_params()
        params.update(
            {
                "action": "search",
                "target": "RESEARCHER",
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

    async def browse_researcher(self, cn: str) -> str:
        """CN으로 연구자 상세 조회. CallAPIInfo(관련 논문/특허/보고서 조회 파라미터) 포함."""
        params = self._build_common_params()
        params.update({"action": "browse", "target": "RESEARCHER", "cn": cn})
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(self.base_url, params=params)
            response.raise_for_status()
            return response.text


def _text(item: dict, code: str) -> str | None:
    sub_items = item.get("item", [])
    if isinstance(sub_items, dict):
        sub_items = [sub_items]
    for sub in sub_items:
        if sub.get("@metaCode") == code:
            return sub.get("#text") or None
    return None


def _parse_int(val: str | None) -> int | None:
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


def _parse_researcher_record(record: dict) -> ResearcherRecord:
    return ResearcherRecord(
        cn=_text(record, "CN"),
        author_name_kor=_text(record, "AuthorNameKor"),
        author_name_eng=_text(record, "AuthorNameEng"),
        author_inst_kor=_text(record, "AuthorInstKor"),
        author_inst_eng=_text(record, "AuthorInstEng"),
        keyword=_text(record, "Keyword"),
        rno=_text(record, "Rno"),
        email=_text(record, "Email"),
        article_cnt=_parse_int(_text(record, "ArticleCnt")),
        patent_cnt=_parse_int(_text(record, "PatentCnt")),
        report_cnt=_parse_int(_text(record, "ReportCnt")),
    )


def _extract_records(parsed: dict) -> list[dict]:
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
    return [r for r in records if isinstance(r, dict)]


def parse_researcher_search_xml(xml: str) -> list[ResearcherRecord]:
    try:
        parsed = xmltodict.parse(xml)
    except Exception:
        return []
    return [_parse_researcher_record(r) for r in _extract_records(parsed)]


def parse_researcher_detail_xml(xml: str) -> tuple[ResearcherRecord | None, list[CallAPIInfo]]:
    try:
        parsed = xmltodict.parse(xml)
    except Exception:
        return None, []

    records = _extract_records(parsed)
    if not records:
        return None, []

    record = records[0]
    researcher = _parse_researcher_record(record)

    call_api_infos: list[CallAPIInfo] = []
    items = record.get("item", [])
    if isinstance(items, dict):
        items = [items]
    for item in items:
        # 실제 응답은 metaCode="CallAPIInfo" (문서상 metaGroupCode와 다름 — 실측 기준)
        if not isinstance(item, dict) or item.get("@metaCode") != "CallAPIInfo":
            continue
        sub_items = item.get("item", [])
        if isinstance(sub_items, dict):
            sub_items = [sub_items]
        parameter_values: dict = {}
        provider_api_id: str | None = None
        provider_api_name: str | None = None
        for sub in sub_items:
            code = sub.get("@metaCode")
            if code == "ProviderAPIId":
                provider_api_id = sub.get("#text")
            elif code == "ProviderAPIName":
                provider_api_name = sub.get("#text")
            elif code == "ParameterValue":
                param_name = sub.get("@ParameterName")
                # searchQuery 값 자체는 ParameterName 속성 없이 오므로(실측 기준) "searchQuery"로 취급
                parameter_values[param_name or "searchQuery"] = sub.get("#text")
        call_api_infos.append(
            CallAPIInfo(
                provider_api_id=provider_api_id,
                provider_api_name=provider_api_name,
                parameter_values=parameter_values,
            )
        )

    return researcher, call_api_infos
