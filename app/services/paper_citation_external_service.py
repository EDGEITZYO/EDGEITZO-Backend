"""코퍼스 밖(in_service=false) 논문의 상세 조회.

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
from app.schemas.paper_citation import PaperCitationExternalDetail

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
    "kci_registered", "enrich_status",
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
        "external_url": external_url,
        "pdf_url": pdf_url,
        "issn": location.get("issn_l") or (issn_list[0] if issn_list else None),
        "publisher": location.get("host_organization_name") or None,
        "is_open_access": (work.get("open_access") or {}).get("is_oa"),
        "enrich_source": "openalex",
    }


_OPENALEX_SELECT = (
    "id,title,abstract_inverted_index,authorships,primary_location,open_access,"
    "publication_year,doi,cited_by_count,keywords"
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


def _cache_key(external_id: str) -> str:
    return f"paper_citation:external_detail:{external_id}"


def _cache_detail(external_id: str, detail: PaperCitationExternalDetail) -> None:
    try:
        get_redis(_REDIS_DB).set(
            _cache_key(external_id),
            detail.model_dump_json(),
            ex=settings.paper_citation_external_detail_cache_ttl_seconds,
        )
    except Exception:
        logger.warning("외부 논문 상세 캐시 저장 실패", exc_info=True)


async def get_external_paper_detail(external_id: str, db: AsyncSession) -> PaperCitationExternalDetail:
    """그래프의 in_service=false 노드를 클릭했을 때 쓰는 상세 조회."""
    try:
        cached = get_redis(_REDIS_DB).get(_cache_key(external_id))
        if cached:
            return PaperCitationExternalDetail(**json.loads(cached))
    except Exception:
        logger.warning("외부 논문 상세 캐시 조회 실패", exc_info=True)

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
        logger.warning("외부 논문 상세 조회 타임아웃: %s", external_id)
        enriched = None

    # 외부 조회 결과를 우선하되, 비어 있는 필드는 저장된 서지정보로 메운다.
    enriched = enriched or {}
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
