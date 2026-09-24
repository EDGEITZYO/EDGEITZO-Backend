"""해외 논문(in_service=false) 노드의 상세 조회.

인용관계/참고문헌 그래프의 노드 중 자체 코퍼스에 없는 논문은 papers 테이블에 적재돼 있지 않아
상세페이지로 갈 수 없었다. 여기서는 저장된 서지정보에 초록·링크를 얹어 돌려준다. 외부 조회가
실패해도 서지정보만으로 응답은 항상 성립한다(enriched=false).

조회 순서:
  1. scripts/enrich_paper_citation_external_refs.py로 **사전 적재된 값**이 있으면 그대로 쓴다
     (외부 호출 0회, 1ms 미만). 대부분의 요청이 여기서 끝난다.
  2. 아직 적재 전인 행만 클릭 시점에 KCI/OpenAlex를 부른다(실측 p90 354ms).

경로별 구성비와 전수 실측 커버리지(2026-08-31, 참고문헌 17,230건 전수):
  - ART… (KCI arti-id 보유, 14.6%)  → KCI articleDetail        초록 97.3% / 키워드 97%
  - DOI 보유(REF…/W…, 18.4%)        → OpenAlex 단건 → S2 폴백   초록 75.3%
  - DOI 없는 REF… (66.9%)           → Crossref로 DOI 역추적 후 위와 동일
     예전엔 "조회 수단 없음"으로 포기하던 구간이다. 제목뿐 아니라 저널·연도·제1저자가 함께
     있어서 Crossref query.bibliographic로 DOI를 되찾을 수 있다. 검증은 Crossref가 주는
     score가 아니라 제목 유사도 0.85로 한다 — score는 "Faculty Opinions recommendation of…"
     류의 추천 레코드를 걸러내지 못했다.

초록 출처가 s2_tldr이면 사람이 쓴 초록이 아니라 AllenAI 모델이 생성한 한 줄 요약이다.
enrich_source로 구분되며, 화면에서 초록과 다르게 표기해야 한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

import httpx
import xmltodict
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.core.redis import get_redis
from app.core.settings import settings
from app.models.paper import PaperCitationExternalRef
from app.schemas.paper_citation import (
    PaperCitationExternalDetail,
    RelatedCorpusPaper,
    RelatedCorpusPapersResponse,
)

logger = logging.getLogger(__name__)

_REDIS_DB = 7
_HANGUL = re.compile(r"[가-힣]")
_DOI_PREFIX = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)
_OPENALEX_ID = re.compile(r"^W\d+$")


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text_of(node: Any) -> str:
    if isinstance(node, dict):
        return (node.get("#text") or "").strip()
    return (node or "").strip()


def _normalize_doi(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    return _DOI_PREFIX.sub("", doi.strip()).lower() or None


def _is_korean(text: str) -> bool:
    """초록 언어 판정. KCI는 @lang을 original/english로만 주기 때문에 original이 한국어인지
    영어인지는 값으로 알 수 없어, 한글 글자수로 직접 판정한다(표본상 original의 40%가 영문)."""
    return len(_HANGUL.findall(text)) > 20


def _user_agent() -> dict[str, str]:
    if settings.openalex_mailto:
        return {"User-Agent": f"edgeitzo/1.0 (mailto:{settings.openalex_mailto})"}
    return {"User-Agent": "edgeitzo/1.0"}


# ---------------------------------------------------------------------------
# 저장된 서지정보 병합
# ---------------------------------------------------------------------------

async def _load_stored_rows(db: AsyncSession, external_id: str) -> list[PaperCitationExternalRef]:
    result = await db.execute(
        select(PaperCitationExternalRef).where(PaperCitationExternalRef.external_id == external_id)
    )
    return list(result.scalars().all())


# 사전 적재(scripts/enrich_paper_citation_external_refs.py)로 채워지는 필드.
# 이 값들이 있으면 외부 API를 부르지 않고 그대로 응답한다.
_ENRICHED_FIELDS = (
    "abstract", "abstract_lang", "abstract_source", "title_en", "keywords", "resolved_doi",
    "external_url", "pdf_url", "citation_count", "publisher", "issn", "is_open_access",
    "kci_registered", "paper_type", "published_at", "enrich_status",
)


def _merge_stored(rows: list[PaperCitationExternalRef]) -> dict[str, Any]:
    """같은 external_id가 여러 source_cn 아래에 있을 수 있고 행마다 채워진 필드가 다르다.
    필드별로 값이 있는 첫 행을 채택해 가장 완전한 하나로 합친다."""
    merged: dict[str, Any] = {
        "title": None, "authors": None, "journal": None, "doi": None,
        "pubyear": None, "external_source": None,
    }
    merged.update({field: None for field in _ENRICHED_FIELDS})
    for row in rows:
        for field in merged:
            if merged[field] is None:
                value = getattr(row, field, None)
                if value:
                    merged[field] = value
    return merged


# Crossref type → 화면 라벨. **해외 논문 상세에서만 쓴다.**
#
# 국내 논문은 db_code에서 파생한 별도 체계를 쓴다
# (credibility_service.resolve_paper_type → paper_type_label). 두 체계를 합치지 않는
# 이유는 코퍼스 1,000편에 단행본·보고서·프리프린트가 아예 없기 때문이다 — 국내 쪽에
# 쓰이지도 않을 라벨을 넣으면 검색 드롭다운과 표시 어휘만 어긋난다.
# 그래서 '학술 대회'도 여기서만 쓴다. 국내 CFKO 3편은 지금처럼 '학술 저널'로 둔다.
#
# DB에는 Crossref 원본을 그대로 저장하고 읽을 때 변환한다. 국내가 db_code를 저장하고
# 라벨을 파생시키는 것과 같은 방식이다 — 매핑 규칙이 바뀌어도 재적재가 필요 없고,
# 19가지 원본 값이 남아 있어 나중에 더 잘게 나눌 수도 있다.
#
# 실측 분포(11,587건): journal-article 94.3% / book-chapter 170 / proceedings-article 160
_CROSSREF_TYPE_LABEL: dict[str, str] = {
    "journal-article":     "학술 저널",
    "proceedings-article": "학술 대회",
    "proceedings":         "학술 대회",
    "dissertation":        "학위논문",
    "book":                "단행본",
    "book-chapter":        "단행본",
    "edited-book":         "단행본",
    "monograph":           "단행본",
    "reference-book":      "단행본",
    "book-series":         "단행본",
    "book-part":           "단행본",
    "book-section":        "단행본",
    "book-set":            "단행본",
    "book-track":          "단행본",
    "report":              "보고서",
    "report-component":    "보고서",
    "standard":            "보고서",
    "posted-content":      "프리프린트",
}


def _paper_type_label(crossref_type: Optional[str]) -> Optional[str]:
    """매핑에 없으면 null을 돌려준다 — '기타'로 뭉뚱그리지 않는다.

    dataset·database·component(그림·표 같은 논문 구성요소)·peer-review(심사보고서)·
    reference-entry 같은 값은 애초에 논문이 아니다(실측 72건). 라벨을 붙이면 논문 목록에
    데이터셋이 논문인 척 섞인다. 값을 비워 화면이 '유형 미상'으로 처리하게 둔다.
    """
    if not crossref_type:
        return None
    return _CROSSREF_TYPE_LABEL.get(crossref_type.strip().lower())


def _detail_from_stored(external_id: str, stored: dict[str, Any]) -> PaperCitationExternalDetail:
    """사전 적재된 값만으로 상세를 만든다. 외부 호출이 없어 1ms 미만이다.

    abstract_source가 's2_tldr'이면 초록이 아니라 AllenAI 모델이 논문 본문에서 뽑은 한 줄
    요약이다. 값 자체는 abstract 필드로 내려가지만 enrich_source로 출처가 구분되므로,
    프런트는 그때 "요약 (Semantic Scholar 자동 생성)"처럼 초록과 다르게 표기해야 한다."""
    doi = _normalize_doi(stored.get("resolved_doi") or stored.get("doi"))
    return PaperCitationExternalDetail(
        key=external_id,
        in_service=False,
        title=stored.get("title"),
        title_en=stored.get("title_en"),
        authors=list(stored["authors"]) if stored.get("authors") else None,
        journal_name=stored.get("journal"),
        pub_year=stored.get("pubyear"),
        doi=doi,
        abstract=stored.get("abstract"),
        abstract_lang=stored.get("abstract_lang"),
        keywords=list(stored["keywords"]) if stored.get("keywords") else None,
        paper_type=_paper_type_label(stored.get("paper_type")),
        published_at=stored.get("published_at"),
        citation_count=stored.get("citation_count"),
        kci_registered=stored.get("kci_registered"),
        external_url=stored.get("external_url") or (f"https://doi.org/{doi}" if doi else None),
        pdf_url=stored.get("pdf_url"),
        issn=stored.get("issn"),
        publisher=stored.get("publisher"),
        is_open_access=stored.get("is_open_access"),
        enriched=bool(stored.get("abstract")),
        enrich_source=stored.get("abstract_source"),
    )


# ---------------------------------------------------------------------------
# KCI (ART… — arti-id 보유 참고문헌)
# ---------------------------------------------------------------------------

async def _fetch_kci(client: httpx.AsyncClient, art_id: str) -> Optional[dict[str, Any]]:
    if not settings.kci_api_key:
        return None
    try:
        response = await client.get(
            settings.kci_base_url,
            params={"apiCode": "articleDetail", "key": settings.kci_api_key, "id": art_id},
        )
        response.raise_for_status()
        record = xmltodict.parse(response.text).get("MetaData", {}).get("outputData", {}).get("record") or {}
    except Exception:
        logger.warning("KCI articleDetail 조회 실패: %s", art_id, exc_info=True)
        return None

    article = record.get("articleInfo") or {}
    if not article:
        return None
    journal = record.get("journalInfo") or {}

    titles = {t.get("@lang"): _text_of(t) for t in _as_list((article.get("title-group") or {}).get("article-title")) if isinstance(t, dict)}
    authors = [a.get("name") for a in _as_list((article.get("author-group") or {}).get("author")) if isinstance(a, dict) and a.get("name")]

    abstracts = [_text_of(a) for a in _as_list((article.get("abstract-group") or {}).get("abstract"))]
    abstracts = [a for a in abstracts if a]
    korean = next((a for a in abstracts if _is_korean(a)), None)
    abstract = korean or (abstracts[0] if abstracts else None)

    # KCI는 같은 키워드를 한/영 그룹으로 두 번 내려주는 경우가 있어 순서를 지키며 중복 제거
    keywords: list[str] = []
    for kw in _as_list((article.get("keyword-group") or {}).get("keyword")):
        text = _text_of(kw) if isinstance(kw, dict) else (kw or "").strip()
        if text and text not in keywords:
            keywords.append(text)

    citation_count = None
    raw_count = article.get("citation-count")
    if isinstance(raw_count, dict):
        try:
            citation_count = int(raw_count.get("@kci") or raw_count.get("#text") or 0)
        except (TypeError, ValueError):
            citation_count = None

    pubyear = None
    try:
        pubyear = int(journal.get("pub-year")) if journal.get("pub-year") else None
    except (TypeError, ValueError):
        pubyear = None

    return {
        "title": titles.get("original") or None,
        "title_en": titles.get("english") or titles.get("foreign") or None,
        "authors": authors or None,
        "journal_name": journal.get("journal-name") or None,
        "pub_year": pubyear,
        "doi": article.get("doi") or None,
        "abstract": abstract,
        "abstract_lang": ("ko" if abstract and _is_korean(abstract) else "en") if abstract else None,
        "keywords": keywords or None,
        "citation_count": citation_count,
        "kci_registered": (journal.get("kci-registration") == "등재") or None,
        "external_url": article.get("url") or None,
        "pdf_url": None,  # KCI articleDetail은 원문 PDF 주소를 주지 않는다(논문 페이지 링크만)
        "issn": journal.get("issn") or None,
        "publisher": journal.get("publisher-name") or None,
        "is_open_access": None,
        "enrich_source": "kci",
    }


# ---------------------------------------------------------------------------
# OpenAlex (W… 또는 DOI 보유 참고문헌)
# ---------------------------------------------------------------------------

def _abstract_from_inverted(inverted: Optional[dict[str, list[int]]]) -> Optional[str]:
    """OpenAlex는 초록을 단어→위치 목록의 역색인으로 준다. 위치 순으로 되돌린다."""
    if not inverted:
        return None
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        for index in indexes:
            positions.append((index, word))
    if not positions:
        return None
    positions.sort()
    return " ".join(word for _, word in positions)


def _openalex_links(work: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(원문 링크, PDF 링크). work["id"]는 OpenAlex 자체 페이지라 사용자에게 보여줄 링크가
    아니다 — 실제 논문에 도달하는 순서로 고른다: OA 원문 > 출판사 랜딩 > DOI."""
    location = work.get("primary_location") or {}
    open_access = work.get("open_access") or {}
    doi = _normalize_doi(work.get("doi"))

    pdf_url = location.get("pdf_url") or None
    external_url = (
        open_access.get("oa_url")
        or pdf_url
        or location.get("landing_page_url")
        or (f"https://doi.org/{doi}" if doi else None)
    )
    return external_url, pdf_url


def _from_openalex_work(work: dict[str, Any]) -> dict[str, Any]:
    abstract = _abstract_from_inverted(work.get("abstract_inverted_index"))
    location = (work.get("primary_location") or {}).get("source") or {}
    external_url, pdf_url = _openalex_links(work)
    issn_list = location.get("issn") or []
    return {
        "title": work.get("title") or None,
        "title_en": work.get("title") or None,
        "authors": [
            a["author"]["display_name"]
            for a in work.get("authorships") or []
            if a.get("author", {}).get("display_name")
        ] or None,
        "journal_name": location.get("display_name") or None,
        "pub_year": work.get("publication_year"),
        "doi": _normalize_doi(work.get("doi")),
        "abstract": abstract,
        "abstract_lang": ("ko" if abstract and _is_korean(abstract) else "en") if abstract else None,
        "keywords": [k["display_name"] for k in work.get("keywords") or [] if k.get("display_name")] or None,
        "citation_count": work.get("cited_by_count"),
        "kci_registered": None,
        "paper_type": work.get("type") or None,
        "published_at": work.get("publication_date") or (
            str(work.get("publication_year")) if work.get("publication_year") else None
        ),
        "external_url": external_url,
        "pdf_url": pdf_url,
        "issn": location.get("issn_l") or (issn_list[0] if issn_list else None),
        "publisher": location.get("host_organization_name") or None,
        "is_open_access": (work.get("open_access") or {}).get("is_oa"),
        "enrich_source": "openalex",
    }


_OPENALEX_SELECT = (
    "id,title,abstract_inverted_index,authorships,primary_location,open_access,"
    "publication_year,publication_date,doi,cited_by_count,keywords,type"
)


def _openalex_id_of(work: dict[str, Any]) -> Optional[str]:
    raw = work.get("id") or ""
    return raw.rsplit("/", 1)[-1] or None


async def _fetch_openalex(client: httpx.AsyncClient, *, work_id: Optional[str] = None, doi: Optional[str] = None) -> Optional[dict[str, Any]]:
    """주의: OpenAlex는 형식이 어긋난 id를 404로 돌려주지 않고 멋대로 정규화해 **다른 논문**을
    반환한다(실측: W000000000000 → W0의 논문). 잘못된 논문 상세를 띄우면 안 되므로 id 형식을
    먼저 거르고, 응답으로 온 식별자가 요청한 것과 같은지 반드시 대조한다."""
    if work_id:
        if not _OPENALEX_ID.match(work_id):
            return None
        path = f"https://api.openalex.org/works/{work_id}"
    elif doi:
        path = f"https://api.openalex.org/works/doi:{doi}"
    else:
        return None
    try:
        response = await client.get(path, params={"select": _OPENALEX_SELECT})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        work = response.json()
    except Exception:
        logger.warning("OpenAlex 조회 실패 (work_id=%s doi=%s)", work_id, doi, exc_info=True)
        return None

    if work_id and _openalex_id_of(work) != work_id:
        logger.warning("OpenAlex가 다른 논문을 반환: 요청 %s → 응답 %s", work_id, _openalex_id_of(work))
        return None
    if doi and _normalize_doi(work.get("doi")) != doi:
        logger.warning("OpenAlex가 다른 DOI를 반환: 요청 %s → 응답 %s", doi, work.get("doi"))
        return None
    return _from_openalex_work(work)


# ---------------------------------------------------------------------------
# 조합
# ---------------------------------------------------------------------------

async def _enrich(external_id: str, stored: dict[str, Any]) -> Optional[dict[str, Any]]:
    """id 형태와 DOI 유무로 조회처를 고른다. KCI가 비면 DOI로 한 번 더 시도한다."""
    doi = _normalize_doi(stored.get("doi"))
    timeout = settings.paper_citation_external_fetch_timeout_seconds

    async with httpx.AsyncClient(timeout=timeout, headers=_user_agent(), follow_redirects=True) as client:
        if external_id.startswith("ART"):
            enriched = await _fetch_kci(client, external_id)
            if enriched:
                return enriched
        elif external_id.startswith("W"):
            enriched = await _fetch_openalex(client, work_id=external_id)
            if enriched:
                return enriched

        if doi:
            return await _fetch_openalex(client, doi=doi)
    return None


# 응답 스키마에 필드를 더하거나 의미를 바꾸면 올린다.
#
# 캐시에는 그 시점 스키마로 직렬화된 JSON이 들어 있고 TTL이 24시간이다. 버전을 안 올리면
# 배포 후에도 최대 하루 동안 **새 필드가 빠진 옛 응답**이 그대로 나간다(Pydantic이 없는
# 필드를 기본값 null로 채우므로 에러도 안 나고 조용히 비어 있다).
# 배포 절차에 "Redis에서 paper_citation:external_detail:* 지우기"를 넣는 방법도 있지만,
# 잊으면 증상이 조용해서 알아차리기 어렵다. 키에 버전을 박아 두면 잊을 수가 없다.
# 옛 키는 참조되지 않은 채 TTL로 알아서 사라진다.
_DETAIL_CACHE_VERSION = "v2"  # v2: paper_type / published_at 추가 (2026-09-24)


def _cache_key(external_id: str) -> str:
    return f"paper_citation:external_detail:{_DETAIL_CACHE_VERSION}:{external_id}"


def _cache_detail(external_id: str, detail: PaperCitationExternalDetail) -> None:
    try:
        get_redis(_REDIS_DB).set(
            _cache_key(external_id),
            detail.model_dump_json(),
            ex=settings.paper_citation_external_detail_cache_ttl_seconds,
        )
    except Exception:
        logger.warning("해외 논문 상세 캐시 저장 실패", exc_info=True)


async def get_external_paper_detail(external_id: str, db: AsyncSession) -> PaperCitationExternalDetail:
    """그래프의 in_service=false 노드를 클릭했을 때 쓰는 상세 조회."""
    try:
        cached = get_redis(_REDIS_DB).get(_cache_key(external_id))
        if cached:
            return PaperCitationExternalDetail(**json.loads(cached))
    except Exception:
        logger.warning("해외 논문 상세 캐시 조회 실패", exc_info=True)

    rows = await _load_stored_rows(db, external_id)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"external paper not found: {external_id}",
        )
    stored = _merge_stored(rows)

    # 사전 적재가 끝난 행이면 외부 호출 없이 바로 응답한다(실측 p90 354ms → 1ms 미만).
    # enrich_status가 'no_abstract'/'no_match'여도 그건 "찾아봤지만 없었다"는 결론이므로
    # 재조회하지 않는다 — 매칭 안 되는 건의 상당수는 EU 법령·정부보고서처럼 애초에
    # 논문이 아니라서 다시 물어도 결과가 같다. 갱신이 필요하면 백필 스크립트를
    # --retry-failed로 다시 돌린다.
    if stored.get("enrich_status"):
        detail = _detail_from_stored(external_id, stored)
        _cache_detail(external_id, detail)
        return detail

    # 아직 적재 전인 행만 기존의 실시간 조회로 폴백한다.
    try:
        enriched = await _enrich(external_id, stored)
    except asyncio.TimeoutError:
        logger.warning("해외 논문 상세 조회 타임아웃: %s", external_id)
        enriched = None

    # 외부 조회 결과를 우선하되, 비어 있는 필드는 저장된 서지정보로 메운다.
    enriched = enriched or {}
    raw_paper_type = enriched.get("paper_type") or stored.get("paper_type")
    detail = PaperCitationExternalDetail(
        key=external_id,
        in_service=False,
        title=enriched.get("title") or stored.get("title"),
        title_en=enriched.get("title_en"),
        authors=enriched.get("authors") or (list(stored["authors"]) if stored.get("authors") else None),
        journal_name=enriched.get("journal_name") or stored.get("journal"),
        pub_year=enriched.get("pub_year") or stored.get("pubyear"),
        doi=enriched.get("doi") or _normalize_doi(stored.get("doi")),
        abstract=enriched.get("abstract"),
        abstract_lang=enriched.get("abstract_lang"),
        keywords=enriched.get("keywords"),
        paper_type=_paper_type_label(raw_paper_type),
        published_at=enriched.get("published_at") or stored.get("published_at"),
        citation_count=enriched.get("citation_count"),
        kci_registered=enriched.get("kci_registered"),
        external_url=enriched.get("external_url") or (
            f"https://doi.org/{_normalize_doi(stored.get('doi'))}" if stored.get("doi") else None
        ),
        pdf_url=enriched.get("pdf_url"),
        issn=enriched.get("issn"),
        publisher=enriched.get("publisher"),
        is_open_access=enriched.get("is_open_access"),
        enriched=bool(enriched),
        enrich_source=enriched.get("enrich_source"),
    )

    _cache_detail(external_id, detail)
    return detail


# ---------------------------------------------------------------------------
# 연관된 코퍼스 논문
# ---------------------------------------------------------------------------

def _related_cache_key(external_id: str) -> str:
    return f"paper_citation:external_related:v1:{external_id}"


def _select_related(hits: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """거리로 자른다. 개수를 먼저 정하지 않는다.

    절대 임계값만 쓰면 같은 0.52가 서로 다른 뜻이 된다. 1위가 0.40인 질의는 진짜 관련
    논문이 있는 경우라 2·3위도 쓸 만한데, 1위가 0.51인 질의는 간신히 걸린 것이라
    2위부터는 대체로 무관하다. 그래서 **1위와의 상대 거리**로 한 번 더 자른다.

    표본 120건 실측(2026-09-24) — 정답은 그 참고문헌을 인용한 코퍼스 논문과의 키워드 겹침:

        절대 0.52 + 고정상한 5건     정밀도 46.5%
        절대 0.52 + 상한 없음        정밀도 37.3%  (최대 20건까지 쏟아짐)
        절대 0.52 + 상대 +0.05       정밀도 43.5%
        위 + 안전상한 10건           정밀도 44.1%  ← 채택

    고정 상한 5건은 근거가 없었다. 순위별 정밀도가 1위 59.6% / 4위 50.0% / 6위 33.3% /
    8위 28.6%로 완만하게 떨어져 5에서 끊을 이유가 없고, 실제 통과 건수도 질의마다
    0~20건으로 크게 다르다(절반은 0건). 안전상한 10건은 화면·페이로드 보호용이고
    10건을 넘는 질의는 3.3%뿐이다.
    """
    passed = [h for h in hits if h[1] <= settings.paper_citation_related_max_distance]
    if not passed:
        return []
    cutoff = passed[0][1] + settings.paper_citation_related_relative_band
    return [h for h in passed if h[1] <= cutoff][: settings.paper_citation_related_limit]


def _embed_and_search(text_to_embed: str, n_results: int) -> list[tuple[str, float]]:
    """검색과 같은 모델·컬렉션을 쓴다. 코퍼스는 'passage: ' 접두로 임베딩돼 있고
    질의는 'query: ' 접두를 붙이는 게 BGE-m3-ko 권장 사용법이라 그대로 맞춘다."""
    import chromadb

    from app.services.embedding_model import get_bge_model

    vector = get_bge_model().encode(f"query: {text_to_embed}", convert_to_numpy=True).tolist()
    collection = chromadb.HttpClient(
        host=settings.chroma_host, port=settings.chroma_port
    ).get_collection("papers")
    result = collection.query(
        query_embeddings=[vector], n_results=n_results, include=["distances"]
    )
    return list(zip(result["ids"][0], result["distances"][0]))


async def get_related_corpus_papers(
    external_id: str, db: AsyncSession
) -> RelatedCorpusPapersResponse:
    """해외 논문과 주제가 가까운 코퍼스 논문을 찾는다.

    적재하지 않는다 — 제목(초록이 있으면 초록까지)을 요청 시점에 임베딩해 검색 코퍼스에서
    가까운 것을 고른다. 비용은 검색 한 번과 같고, 코퍼스가 바뀌면 결과도 따라 바뀐다.

    빈 배열이 정상 응답이다. 코퍼스가 1,000편뿐이라 관련 논문이 아예 없는 해외 문헌이
    절반가량이고(실측 52.5%), 그중 상당수는 정부 연차보고서·교육과정 문서·법령이라
    애초에 논문이 아니다. 임계값 없이 상위 5건을 그냥 내보내면 정밀도가 59.2%까지
    떨어진다 — 5건 중 2건이 무관해진다.
    """
    try:
        cached = get_redis(_REDIS_DB).get(_related_cache_key(external_id))
        if cached:
            return RelatedCorpusPapersResponse(**json.loads(cached))
    except Exception:
        logger.warning("연관 논문 캐시 조회 실패", exc_info=True)

    rows = await _load_stored_rows(db, external_id)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"external paper not found: {external_id}",
        )
    stored = _merge_stored(rows)

    title = stored.get("title_en") or stored.get("title")
    if not title:
        return RelatedCorpusPapersResponse(external_id=external_id, items=[], used_abstract=False)

    abstract = stored.get("abstract")
    # s2_tldr은 사람이 쓴 초록이 아니라 모델 요약이지만, 주제를 담고 있어 검색에는 쓸모가 있다.
    used_abstract = bool(abstract)
    query_text = f"{title} {abstract}" if abstract else title

    # 상대 기준으로 다시 자르므로 임계값 통과분을 넉넉히 받아와야 한다.
    fetch = max(settings.paper_citation_related_limit * 2, 20)
    try:
        hits = await asyncio.to_thread(_embed_and_search, query_text, fetch)
    except Exception:
        # 연관 논문은 부가 기능이다. 실패해도 상세 화면 전체를 막지 않는다.
        logger.warning("연관 논문 검색 실패 external_id=%s", external_id, exc_info=True)
        return RelatedCorpusPapersResponse(external_id=external_id, items=[], used_abstract=used_abstract)

    selected = _select_related(hits)
    items: list[RelatedCorpusPaper] = []
    if selected:
        meta = await _load_corpus_meta(db, [pid for pid, _ in selected])
        for paper_id, distance in selected:
            row = meta.get(paper_id)
            items.append(
                RelatedCorpusPaper(
                    paper_id=paper_id,
                    title=row.title if row else None,
                    journal_name=row.journal_name if row else None,
                    pub_year=row.pubyear if row else None,
                    distance=round(float(distance), 4),
                )
            )

    response = RelatedCorpusPapersResponse(
        external_id=external_id, items=items, used_abstract=used_abstract
    )
    try:
        get_redis(_REDIS_DB).set(
            _related_cache_key(external_id),
            response.model_dump_json(),
            ex=settings.paper_citation_related_cache_ttl_seconds,
        )
    except Exception:
        logger.warning("연관 논문 캐시 저장 실패", exc_info=True)
    return response


async def _load_corpus_meta(db: AsyncSession, paper_ids: list[str]):
    """Chroma는 id와 거리만 준다. 화면에 뿌릴 제목·학술지·연도는 Postgres에서 한 번에 읽는다."""
    from sqlalchemy import text as sa_text

    rows = (
        await db.execute(
            sa_text("SELECT id, title, journal_name, pubyear FROM papers WHERE id = ANY(:ids)"),
            {"ids": paper_ids},
        )
    ).all()
    return {row.id: row for row in rows}
