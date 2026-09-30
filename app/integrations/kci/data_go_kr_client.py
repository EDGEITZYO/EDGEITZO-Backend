"""공공데이터포털 "한국연구재단_KCI 논문정보서비스" — 저자 번호(CRT…) 조회.

KCI Open API(open.kci.go.kr)는 저자 이름·소속 문자열만 주고 KCI 저자 번호는 주지 않는다.
번호는 이 서비스의 두 오퍼레이션에서만 받을 수 있다(기술문서 v6 기준, 2026-09-29 실측):
  openApiD311List  KCI논문저자 조회 — artiId → 논문의 저자 번호·소속. artiId 없이 전체 목록은 비어 온다
  openApiM330List  저자 정보 조회   — certNm(저자명) → 그 이름의 모든 저자 번호·소속·영문명·KRI ID

응답이 느리다(실측 20~40초). 초당 30건까지 허용되므로 호출부에서 동시에 보낸다.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from app.core.settings import settings

BASE_URL = "https://apis.data.go.kr/B552540/KCIOpenApi/artiInfo/"


class QuotaExceeded(Exception):
    """하루 호출 한도 초과. 그날은 더 부르지 말아야 한다."""


class DataGoKrError(Exception):
    pass


@dataclass
class Page:
    total: int
    items: list[dict[str, str]] = field(default_factory=list)


# 공공데이터포털 게이트웨이 오류는 <OpenAPI_ServiceResponse>로 따로 온다.
# 22 = LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR
_QUOTA_CODES = {"22"}


def parse_page(xml_text: str) -> Page:
    gateway = re.search(r"<returnReasonCode>(\d+)</returnReasonCode>", xml_text)
    if gateway:
        code = gateway.group(1)
        msg = re.search(r"<returnAuthMsg>([^<]*)</returnAuthMsg>", xml_text)
        if code in _QUOTA_CODES:
            raise QuotaExceeded(msg.group(1) if msg else code)
        raise DataGoKrError(f"gateway {code}: {msg.group(1) if msg else ''}")
    root = ET.fromstring(xml_text)
    code = (root.findtext("header/resultCode") or "").strip()
    if code in _QUOTA_CODES:
        raise QuotaExceeded(root.findtext("header/resultMsg") or code)
    if code != "00":
        raise DataGoKrError(f"{code}: {root.findtext('header/resultMsg')}")
    total = int(root.findtext("body/totalCount") or 0)
    items = [
        {child.tag: (child.text or "").strip() for child in item}
        for item in root.findall("body/items/item")
    ]
    return Page(total=total, items=items)


class KciDataGoKrClient:
    def __init__(self, http: httpx.AsyncClient, key: str | None = None):
        self._http = http
        self._key = key or settings.kci_data_go_kr_key
        if not self._key:
            raise DataGoKrError("KCI_DATA_GO_KR_KEY가 비어 있다")

    async def _get(self, op: str, **params) -> Page:
        # httpx 오류 메시지에는 serviceKey가 든 URL 전체가 들어간다. 로그에 키가 남지 않도록
        # 여기서 키 없는 메시지로 바꿔 던진다.
        try:
            resp = await self._http.get(BASE_URL + op, params={"serviceKey": self._key, **params})
        except httpx.HTTPError as exc:
            raise DataGoKrError(f"{op} {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise DataGoKrError(f"{op} HTTP {resp.status_code}")
        return parse_page(resp.text)

    async def article_authors(self, arti_id: str, *, page: int = 1, size: int = 100) -> Page:
        return await self._get("openApiD311List", artiId=arti_id, pageNo=page, recordCnt=size)

    async def authors_by_name(self, name: str, *, page: int = 1, size: int = 300) -> Page:
        return await self._get("openApiM330List", certNm=name, pageNo=page, recordCnt=size)
