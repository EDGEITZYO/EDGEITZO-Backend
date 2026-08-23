"""KCI Open API — 연구자 적재용 확장 (articleDetail 저자정보 / articleSearch 저자별 논문).

기존 client.py는 articleDetail의 <reference>만 파싱한다. 여기서는 같은 API의
<author-group>과 articleSearch를 다룬다 — 실측으로 확인된 두 응답의 차이:

  articleDetail  구조화된 저자 (name / name-eng / institution / author-part) + keyword-group.
                 단, <reference> 안에도 빈 <author> 태그가 수십 개 들어있어
                 articleInfo/author-group으로 스코프를 좁히지 않으면 빈 저자가 딸려온다.
  articleSearch  저자는 "홍길동(소속)" 인라인 문자열. 키워드는 없지만
                 <citation-count kci="35" wos="0">가 같이 와서 피인용을 공짜로 얻는다.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"
_MAX_DISPLAY = 100  # 가이드 기준 displayCount 최대값

# 소속 문자열에서 기관 단위를 떼어낼 접미사.
# 규칙: **가장 이른 시작 위치**를 찾고, 그 위치에서 **가장 긴 접미사**를 쓴다.
# 두 가지를 동시에 피해야 해서 이렇게 됐다(둘 다 실측으로 겪은 실패다):
#   최장일치만 하면  "한서대학교 문화재보존과학연구센터" → 뒤의 '센터'가 이겨 통째로 남는다.
#                    그 값이 KCI affiliation 필터에 들어가 논문이 1편만 잡혔다.
#   최초종료만 하면  "연세대학교" → '대학'이 '대학교'보다 먼저 끝나 '연세대학'으로 잘린다.
_INST_SUFFIXES = (
    "대학교병원", "대학교", "대학병원", "대학", "연구원", "연구소", "병원", "학회", "재단",
    "공사", "공단", "센터", "진흥원", "진흥청", "과학원", "기술원", "연구회", "협회",
    "주식회사", "농업기술원", "보건환경연구원", "사업단", "연구단", "박물관", "과학관",
)


def _api_key() -> str:
    return settings.kci_api_key or "64625154"


def institution_root(raw: str | None) -> str | None:
    """'청운대학교 식품영양학과' → '청운대학교', '한서대학교 문화재보존과학연구센터' → '한서대학교'."""
    if not raw:
        return None
    first = re.split(r"[,/]", raw)[0].strip()
    if not first:
        return None

    best_start: int | None = None
    best_end: int | None = None
    for suffix in _INST_SUFFIXES:
        start = first.find(suffix)
        if start < 0:
            continue
        end = start + len(suffix)
        # 더 앞에서 시작하면 교체, 같은 자리에서 시작하면 더 긴 쪽을 쓴다.
        if best_start is None or start < best_start or (start == best_start and end > best_end):
            best_start, best_end = start, end

    if best_end:
        return first[:best_end].strip()
    # 접미사가 없는 경우. 영문 소속("Yonsei University")은 첫 토큰만 떼면 "Yonsei"가 되어
    # 기관명이 아니게 된다 — 조각 전체가 이미 기관명이므로 그대로 쓴다.
    if not re.search(r"[가-힣]", first):
        return first
    return first.split()[0].strip()


# 학위논문 소속은 "한양대학교 대학원"처럼 끝난다. 여기서 떼어낸 "대학원"은 전공이 아니라
# 그냥 소속의 나머지다 — 명세서의 「전공」 자리에 넣으면 잘못된 정보가 된다.
_NOT_A_MAJOR = frozenset({
    "대학원", "일반대학원", "산업대학원", "특수대학원", "전문대학원", "University", "College",
    "Graduate School", "대학", "학과", "과",
})


def institution_dept(raw: str | None) -> str | None:
    """기관 루트를 제외한 나머지(학과/부서). 동명이인 구분 표시용."""
    if not raw:
        return None
    first = re.split(r"[,/]", raw)[0].strip()
    root = institution_root(first)
    if not root:
        return None
    rest = first[len(root):].strip() if first.startswith(root) else first.replace(root, "").strip()
    if not rest or rest in _NOT_A_MAJOR:
        return None
    return rest


@dataclass
class KCIAuthor:
    name: str
    name_eng: str | None = None
    institution: str | None = None
    role: str | None = None  # 제1 | 교신 | 참여 | 단독
    order: int = 0


@dataclass
class KCIArticle:
    art_id: str
    title: str | None = None
    title_eng: str | None = None
    journal: str | None = None
    pubyear: int | None = None
    pubmonth: str | None = None
    citation_count: int = 0
    categories: list[str] = field(default_factory=list)
    doi: str | None = None
    url: str | None = None
    authors: list[KCIAuthor] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    # articleDetail에서만 오는 서지 지표.
    # fwci = Field-Weighted Citation Impact(분야 평균 1.0 기준 보정 피인용).
    fwci: float | None = None
    language: str | None = None
    regularity: str | None = None       # 'Y' | 'N' — 정규 논문 여부
    kci_registration: str | None = None  # 등재 | 등재후보 | 우수등재


def _float_or_none(raw: str | None) -> float | None:
    try:
        return float(raw) if raw and raw.strip() else None
    except ValueError:
        return None


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(raw) if raw and raw.strip() else None
    except ValueError:
        return None


def parse_article_detail(xml_text: str) -> KCIArticle | None:
    """articleDetail 응답 → 구조화된 저자·키워드를 담은 KCIArticle."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("KCI articleDetail 파싱 실패: %s", exc)
        return None

    info = root.find(".//articleInfo")
    if info is None:
        return None

    journal_info = root.find(".//journalInfo")
    article = KCIArticle(
        art_id=(info.get("article-id") or "").strip(),
        title=_title(info, "original"),
        title_eng=_title(info, "english"),
        journal=(journal_info.findtext("journal-name") or "").strip() or None if journal_info is not None else None,
        pubyear=_int_or_none(journal_info.findtext("pub-year")) if journal_info is not None else None,
        pubmonth=(journal_info.findtext("pub-mon") or "").strip() or None if journal_info is not None else None,
        categories=[c.strip() for c in (info.findtext("article-categories") or "").split(",") if c.strip()],
        doi=(info.findtext("doi") or "").strip() or None,
        fwci=_float_or_none(info.findtext("fwci")),
        language=(info.findtext("article-language") or "").strip() or None,
        regularity=(info.findtext("article-regularity") or "").strip() or None,
        kci_registration=(journal_info.findtext("kci-registration") or "").strip() or None
        if journal_info is not None
        else None,
    )

    # 참고문헌 영역에도 <author>가 있으므로 author-group 안으로만 내려간다.
    group = info.find("author-group")
    if group is not None:
        for idx, node in enumerate(group.findall("author"), start=1):
            name = (node.findtext("name") or "").strip()
            if not name:
                continue
            article.authors.append(
                KCIAuthor(
                    name=name,
                    name_eng=(node.findtext("name-eng") or "").strip() or None,
                    institution=(node.findtext("institution") or "").strip() or None,
                    role=node.get("author-part"),
                    order=idx,
                )
            )

    keyword_group = info.find("keyword-group")
    if keyword_group is not None:
        for node in keyword_group.findall("keyword"):
            raw = (node.text or "").strip()
            if not raw:
                continue
            # 한 태그에 ' · '로 여러 개가 묶여 오는 경우가 있다(실측).
            # 구분자가 U+00B7(MIDDLE DOT)이 아니라 U+0387(GREEK ANO TELEIA)로 오는 논문이 있다 —
            # 눈으로는 구별이 안 되므로 중점 계열을 모두 구분자로 취급한다.
            article.keywords.extend(
                cleaned for cleaned in (clean_keyword(t) for t in _MIDDLE_DOTS.split(raw)) if cleaned
            )

    return article


def _title(info: ET.Element, lang: str) -> str | None:
    group = info.find("title-group")
    if group is None:
        return None
    for node in group.findall("article-title"):
        if node.get("lang") == lang:
            return (node.text or "").strip() or None
    return None


# 2000년대 초 KCI 레코드에는 초록 본문이 통째로 <keyword>에 들어간 것이 섞여 있다
# (실측: 400자짜리 "키워드"가 DB 컬럼 길이를 넘겨 적재를 중단시켰다). 점만 있는 값도 나온다.
_KEYWORD_MAX_LEN = 60  # 코퍼스 실측 평균 13자 — 60자를 넘으면 키워드가 아니라 문장이다
_HAS_CONTENT = re.compile(r"[0-9A-Za-z가-힣]")
_SENTENCE_TAIL = re.compile(r"(?:하였다|되었다|있다|한다|이다|였다|합니다)[.\s]")


def clean_keyword(raw: str | None) -> str | None:
    """키워드로 쓸 수 없는 값을 걸러낸다. 통과하면 정리된 문자열, 아니면 None."""
    if not raw:
        return None
    value = raw.strip().strip(".,;·").strip()
    if not value or len(value) > _KEYWORD_MAX_LEN:
        return None
    if not _HAS_CONTENT.search(value):
        return None
    if _SENTENCE_TAIL.search(value + " "):
        return None
    return value


_MIDDLE_DOTS = re.compile(r"[\u00b7\u0387\u2022\u30fb\u318d]")


_AUTHOR_INLINE = re.compile(r"^(?P<name>[^(]+)(?:\((?P<inst>.*)\))?$")


def parse_article_search(xml_text: str) -> tuple[int, list[KCIArticle]]:
    """articleSearch 응답 → (총건수, 논문 목록). 저자는 '이름(소속)' 인라인 문자열."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("KCI articleSearch 파싱 실패: %s", exc)
        return 0, []

    total = _int_or_none(root.findtext(".//outputData/result/total")) or 0
    articles: list[KCIArticle] = []

    for record in root.findall(".//outputData/record"):
        info = record.find("articleInfo")
        if info is None:
            continue
        journal_info = record.find("journalInfo")

        citation_node = info.find("citation-count")
        citation_count = 0
        if citation_node is not None:
            # kci 속성이 국내 등재지 기준 피인용. 본문 값은 kci+wos 혼합이라 쓰지 않는다.
            citation_count = _int_or_none(citation_node.get("kci")) or 0

        article = KCIArticle(
            art_id=(info.get("article-id") or "").strip(),
            title=_title(info, "original"),
            title_eng=_title(info, "english"),
            journal=(journal_info.findtext("journal-name") or "").strip() or None if journal_info is not None else None,
            pubyear=_int_or_none(journal_info.findtext("pub-year")) if journal_info is not None else None,
            pubmonth=(journal_info.findtext("pub-mon") or "").strip() or None if journal_info is not None else None,
            citation_count=citation_count,
            categories=[c.strip() for c in (info.findtext("article-categories") or "").split(",") if c.strip()],
            doi=(info.findtext("doi") or "").strip() or None,
            url=(info.findtext("url") or "").strip() or None,
        )

        group = info.find("author-group")
        if group is not None:
            for idx, node in enumerate(group.findall("author"), start=1):
                raw = (node.text or "").strip()
                if not raw:
                    continue
                match = _AUTHOR_INLINE.match(raw)
                if not match:
                    continue
                article.authors.append(
                    KCIAuthor(
                        name=match.group("name").strip(),
                        name_eng=node.get("english"),
                        institution=(match.group("inst") or "").strip() or None,
                        order=idx,
                    )
                )
        articles.append(article)

    return total, articles


class KCIResearcherClient:
    """연구자 적재 전용 KCI 호출. 429 백오프는 호출 측(스크립트)에서 관리한다."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def article_detail(self, art_id: str) -> KCIArticle | None:
        resp = await self._client.get(
            _BASE_URL, params={"apiCode": "articleDetail", "key": _api_key(), "id": art_id}
        )
        resp.raise_for_status()
        return parse_article_detail(resp.text)

    async def search_by_author(
        self,
        author: str,
        *,
        affiliation: str | None = None,
        page: int = 1,
        display_count: int = _MAX_DISPLAY,
    ) -> tuple[int, list[KCIArticle]]:
        params = {
            "apiCode": "articleSearch",
            "key": _api_key(),
            "author": author,
            "page": page,
            "displayCount": min(display_count, _MAX_DISPLAY),
            "sortNm": "pubiYr",
            "sortDir": "desc",
        }
        if affiliation:
            params["affiliation"] = affiliation
        resp = await self._client.get(_BASE_URL, params=params)
        resp.raise_for_status()
        return parse_article_search(resp.text)

    async def search_by_title(self, title: str, *, display_count: int = 5) -> tuple[int, list[KCIArticle]]:
        resp = await self._client.get(
            _BASE_URL,
            params={"apiCode": "articleSearch", "key": _api_key(), "title": title, "displayCount": display_count},
        )
        resp.raise_for_status()
        return parse_article_search(resp.text)
