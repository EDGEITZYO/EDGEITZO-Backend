"""코퍼스 밖 참고문헌(paper_citation_external_refs)에 초록·DOI·원문링크를 사전 적재.

지금까지는 그래프 노드를 클릭할 때마다 KCI/OpenAlex를 실시간 호출했다. 그 방식은 느리고
(실측 p90 354ms), 무엇보다 참고문헌의 66.9%는 DOI도 arti-id도 없어 조회할 방법이 아예 없었다.
이 스크립트는 그 값을 미리 채워 런타임 외부 호출을 0으로 만든다.

경로는 세 갈래고, 각 갈래의 커버리지는 전부 실측값이다(2026-08-31):

  ART…            2,524건(14.6%)  KCI articleDetail        초록 96.7%  키워드 96.7%  링크 100%
  REF… + DOI      3,176건(18.4%)  OpenAlex 단건조회         초록 67.4%(tldr 포함 78.8%)  링크 100%
  REF… DOI 없음  11,530건(66.9%)  Crossref 매칭 → OpenAlex  초록 46.6%(tldr 포함 59.2%)  링크 71.7%

DOI 없는 건이 핵심이다. 제목만으로 Semantic Scholar에 물으면 67.5%가 매칭되지만 S2는 초록이
법적 제약으로 막혀 있어(3.3%) 실익이 적었다. 대신 Crossref query.bibliographic에 제목+저널+
연도+제1저자를 통째로 넘기면 71.7%가 매칭되고, 거기서 얻은 DOI로 OpenAlex에 물으면 초록이
훨씬 잘 나온다(OpenAlex는 초록에 법적 제약이 없다). 즉 "매칭은 Crossref, 초록은 OpenAlex"로
역할을 나누는 게 요점이다.

매칭 검증은 Crossref score가 아니라 제목 유사도로 한다. score는 203점 같은 값을 주면서도
"Faculty Opinions recommendation of…" 류의 추천 레코드를 걸러내지 못했다. difflib 정규화
유사도 0.85 이상만 채택한다.

재시작: enrich_status가 null인 행만 처리하므로 중단 후 다시 돌리면 이어서 진행된다.
별도 체크포인트 파일이 없다(DB가 곧 체크포인트).

사용법:
  python scripts/enrich_paper_citation_external_refs.py --dry-run --limit 20
  python scripts/enrich_paper_citation_external_refs.py --path art        # 특정 경로만
  python scripts/enrich_paper_citation_external_refs.py                   # 전체
  python scripts/enrich_paper_citation_external_refs.py --retry-failed    # 실패건 재시도
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.services.paper_citation_external_service import (  # noqa: E402
    _abstract_from_inverted,
    _fetch_kci,
    _is_korean,
    _normalize_doi,
    _openalex_links,
    _user_agent,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("enrich")

# 제목 유사도 채택 기준. 0.85는 실측에서 오매칭(추천 레코드/다른 논문)을 걸러내면서
# 대소문자·구두점 차이만 있는 정상 매칭은 통과시킨 값이다.
TITLE_SIMILARITY_THRESHOLD = 0.85

# API별 동시 요청 수. 서버가 알려주는 값에 맞춘다 — 추측하지 않는다.
# Crossref는 응답 헤더로 한도를 직접 알려준다(x-rate-limit-limit / x-concurrency-limit).
# 실측: User-Agent에 mailto가 있으면 3 req/s·동시 3, 없으면 **1 req/s·동시 1**로 떨어진다.
# 그래서 OPENALEX_MAILTO가 비어 있으면 아래 run()에서 아예 중단시킨다 — 익명 풀로 대량
# 조회하면 429만 받고 전량 실패한다(그렇게 1,700건을 날린 적이 있다).
CONCURRENCY = {"kci": 1, "openalex": 4, "crossref": 3, "s2": 1}

# 초당 허용 건수. 동시성만 제한하면 응답이 빠를 때 순간 속도가 한도를 넘으므로
# 간격도 함께 지킨다.
RATE_PER_SEC = {"crossref": 3.0, "s2": 1.0 / 1.05}


def _date_parts_depth(block: Optional[dict[str, Any]]) -> int:
    """Crossref 날짜 블록이 몇 자리까지 알려주는지. 0=없음, 1=연, 2=연월, 3=연월일."""
    parts = (block or {}).get("date-parts") or [[]]
    if not parts or not parts[0]:
        return 0
    return len([x for x in parts[0] if x is not None])


def _published_at_from_crossref(message: dict[str, Any]) -> Optional[str]:
    """Crossref 레코드에서 발행일을 꺼낸다. **자리를 채우지 않는다.**

    "2007-04-15" / "2007-04" / "2007" / None 중 하나를 그대로 돌려준다.
    표본 45건 실측(2026-09-24): 연월일 46.7% · 연월 48.9% · 연도만 2.2% · 없음 2.2%.
    학술지가 "2007년 4월호"로 내고 일자를 안 밝히는 게 흔해 연월이 절반이다. 원본에
    일자가 없는 것이므로 01을 채우면 그 48.9%에 사실이 아닌 날짜를 띄우게 된다.

    issued가 기준이다. published-print / published-online은 **issued와 같은 해일 때만**
    더 정밀한 값으로 채택한다.

    연도 조건이 핵심이다. 옛 논문은 published-online이 실제 발행일이 아니라 **전자화
    등록일**인 경우가 있다. 실측(2026-09-24):

        10.1111/j.1749-7345.1994.tb00811.x
            issued           [1994, 3]      ← 맞는 값
            published-online [2007, 4, 3]   ← Wiley 백파일 전자화 날짜

    연도를 안 보고 "더 정밀한 쪽"만 고르면 1994년 논문이 2007년으로 바뀐다. 50건
    시범 실행에서 6건이 기존 pubyear와 어긋났고 그중 3건이 이 경우였다.
    같은 해 안에서 자릿수만 늘리는 건 안전하므로 그 경우에만 채택한다.
    """
    issued_depth = _date_parts_depth(message.get("issued"))
    issued_year = (
        message["issued"]["date-parts"][0][0] if issued_depth else None
    )
    best_block, best_depth = message.get("issued"), issued_depth
    for field in ("published-print", "published-online", "published"):
        depth = _date_parts_depth(message.get(field))
        if depth <= best_depth:
            continue
        year = message[field]["date-parts"][0][0]
        # issued가 아예 없으면 비교할 기준이 없으니 그대로 쓴다.
        if issued_year is not None and year != issued_year:
            continue
        best_block, best_depth = message.get(field), depth
    if not best_depth:
        return None
    parts = [x for x in best_block["date-parts"][0] if x is not None][:3]
    out = f"{int(parts[0]):04d}"
    if len(parts) >= 2:
        out += f"-{int(parts[1]):02d}"
    if len(parts) >= 3:
        out += f"-{int(parts[2]):02d}"
    return out


def _norm_title(value: Optional[str]) -> str:
    value = re.sub(r"[^a-z0-9 ]", " ", (value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


def _title_similarity(a: Optional[str], b: Optional[str]) -> float:
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass
class Ref:
    """external_id 하나에 대응하는 작업 단위. 같은 external_id가 여러 source_cn 아래
    중복될 수 있으므로(18,093행 / 17,924 고유) 조회는 한 번만 하고 결과를 전부에 쓴다."""

    external_id: str
    title: Optional[str]
    journal: Optional[str]
    pubyear: Optional[int]
    first_author: Optional[str]
    doi: Optional[str]

    @property
    def path(self) -> str:
        if self.external_id.startswith("ART"):
            return "art"
        if self.doi:
            return "doi"
        return "match"


@dataclass
class Stats:
    total: int = 0
    ok: int = 0
    no_abstract: int = 0
    no_match: int = 0
    error: int = 0
    by_source: dict = field(default_factory=dict)

    def bump(self, source: Optional[str]) -> None:
        if source:
            self.by_source[source] = self.by_source.get(source, 0) + 1


# ---------------------------------------------------------------------------
# 조회 경로
# ---------------------------------------------------------------------------

async def _openalex_by(client: httpx.AsyncClient, *, doi: str) -> Optional[dict[str, Any]]:
    """DOI 단건 조회. OpenAlex는 filter/list 엔드포인트에 크레딧을 물리기 시작했지만
    단건 조회(works/doi:…)는 잔액 0에서도 동작하는 것을 실측 확인했다."""
    try:
        response = await client.get(
            f"https://api.openalex.org/works/doi:{doi}",
            params={
                "select": "id,title,abstract_inverted_index,authorships,primary_location,"
                "open_access,publication_year,doi,cited_by_count,keywords,type"
            },
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        work = response.json()
    except Exception:
        logger.debug("OpenAlex 조회 실패 doi=%s", doi, exc_info=True)
        return None

    # 요청한 DOI와 응답이 같은지 대조한다 — OpenAlex는 형식이 어긋난 식별자를 404로 주지 않고
    # 멋대로 정규화해 다른 논문을 돌려주는 사례가 있다.
    if _normalize_doi(work.get("doi")) != doi:
        logger.warning("OpenAlex가 다른 DOI 반환: 요청 %s → 응답 %s", doi, work.get("doi"))
        return None

    abstract = _abstract_from_inverted(work.get("abstract_inverted_index"))
    external_url, pdf_url = _openalex_links(work)
    source = (work.get("primary_location") or {}).get("source") or {}
    issn_list = source.get("issn") or []
    return {
        "title_en": work.get("title") or None,
        "abstract": abstract,
        "abstract_source": "openalex" if abstract else None,
        "keywords": [k["display_name"] for k in work.get("keywords") or [] if k.get("display_name")] or None,
        "citation_count": work.get("cited_by_count"),
        "external_url": external_url,
        "pdf_url": pdf_url,
        "publisher": source.get("host_organization_name") or None,
        "issn": source.get("issn_l") or (issn_list[0] if issn_list else None),
        "is_open_access": (work.get("open_access") or {}).get("is_oa"),
        # Crossref type이 없을 때의 폴백. OpenAlex는 'article' 같은 자체 어휘를 쓰므로
        # Crossref('journal-article')와 값이 다르다 — 둘을 섞지 않도록 우선순위를 지킨다.
        "paper_type_openalex": work.get("type") or None,
        # OpenAlex publication_date는 쓰지 않는다. 원본에 일자가 없어도 01을 채워 넣은
        # 경우가 있어 "발행일"로 믿을 수 없다. 발행일은 Crossref만 기준으로 삼는다.
    }


async def _crossref_match(
    client: httpx.AsyncClient, limiter: "_RateLimiter", ref: Ref
) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """제목+저널+연도+저자를 한 문자열로 넘겨 DOI를 역으로 찾는다.
    채택 여부는 Crossref가 주는 score가 아니라 제목 유사도로 판정한다.

    반환은 (doi, paper_type, published_at). 예전에는 doi만 돌려줬는데, 매칭에 성공한
    그 응답 안에 type과 issued가 이미 들어 있다 — 버리고 나중에 다시 부르면 호출이
    두 배가 된다. 매칭 실패면 None."""
    if not ref.title:
        return None
    query = " ".join(
        str(x) for x in (ref.first_author, ref.title, ref.journal, ref.pubyear) if x
    )
    items: list = []
    for attempt in range(3):
        await limiter.wait()
        try:
            response = await client.get(
                "https://api.crossref.org/works",
                params={
                    "query.bibliographic": query,
                    "rows": 3,
                    # type·issued·published-*를 같이 받는다. 매칭된 그 레코드가 곧
                    # 발행일·유형의 출처라, 여기서 안 받으면 DOI로 한 번 더 불러야 한다.
                    "select": "DOI,title,type,issued,published-print,published-online",
                },
            )
        except Exception:
            logger.warning("Crossref 요청 예외 %s", ref.external_id, exc_info=True)
            return None
        if response.status_code == 429:
            # 한도를 넘겼다. 지수적으로 물러난다 — 여기서 그냥 실패로 처리하면
            # "매칭 안 되는 논문"과 "우리가 너무 빨리 부른 것"이 구분되지 않는다.
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        if response.status_code != 200:
            logger.warning("Crossref %s → HTTP %s", ref.external_id, response.status_code)
            return None
        items = response.json().get("message", {}).get("items", [])
        break
    else:
        logger.warning("Crossref 429 반복으로 포기: %s", ref.external_id)
        return None

    best_item, best_score = None, 0.0
    for item in items:
        candidate = (item.get("title") or [""])[0]
        score = _title_similarity(ref.title, candidate)
        if score > best_score:
            best_score, best_item = score, item
    if best_item and best_item.get("DOI") and best_score >= TITLE_SIMILARITY_THRESHOLD:
        return (
            _normalize_doi(best_item["DOI"]),
            best_item.get("type") or None,
            _published_at_from_crossref(best_item),
        )
    return None


async def _crossref_by_doi(
    client: httpx.AsyncClient, limiter: "_RateLimiter", doi: str
) -> Optional[tuple[Optional[str], Optional[str]]]:
    """DOI 단건 조회로 (paper_type, published_at)만 가져온다. --backfill-dates 전용.

    이미 DOI를 아는 행이라 제목 유사도 매칭을 할 이유가 없다. 단건 조회는 정확하고,
    query.bibliographic처럼 후보 3건을 받아 비교할 필요도 없다.

    단건 엔드포인트(/works/{doi})는 select 파라미터를 받지 않는다 — 붙이면 요청이
    실패한다(실측). 전체 레코드를 받아 필요한 칸만 꺼낸다.
    """
    for attempt in range(3):
        await limiter.wait()
        try:
            response = await client.get(
                "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
            )
        except Exception:
            logger.warning("Crossref 단건조회 예외 doi=%s", doi, exc_info=True)
            return None
        if response.status_code == 404:
            return None
        if response.status_code == 429:
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        if response.status_code != 200:
            logger.warning("Crossref doi=%s → HTTP %s", doi, response.status_code)
            return None
        try:
            message = response.json()["message"]
        except Exception:
            return None
        return (message.get("type") or None, _published_at_from_crossref(message))
    logger.warning("Crossref 429 반복으로 포기: doi=%s", doi)
    return None


class _RateLimiter:
    """전역 호출 간격을 지킨다. 동시성 세마포어와 함께 쓴다 — 세마포어는 '동시에 몇 개',
    이쪽은 '초당 몇 개'를 담당한다. 둘 중 하나만으로는 한도를 못 지킨다."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._interval:
                await asyncio.sleep(self._interval - gap)
            self._last = time.monotonic()


async def _s2_fallback(client: httpx.AsyncClient, limiter: "_RateLimiter", doi: str) -> tuple[Optional[str], Optional[str]]:
    """OpenAlex에 초록이 없을 때만 부른다. 반환은 (텍스트, 출처).
    출처가 's2_tldr'이면 사람이 쓴 초록이 아니라 AllenAI 모델이 만든 요약이다."""
    headers = {"x-api-key": settings.semantic_scholar_api_key} if settings.semantic_scholar_api_key else {}
    for attempt in range(2):
        await limiter.wait()
        try:
            response = await client.get(
                f"{settings.semantic_scholar_base_url}/paper/DOI:{doi}",
                params={"fields": "abstract,tldr"},
                headers=headers,
            )
            if response.status_code == 429:
                await asyncio.sleep(2.0 * (attempt + 1))
                continue
            if response.status_code != 200:
                return None, None
            data = response.json()
            if data.get("abstract"):
                return data["abstract"], "s2"
            tldr = (data.get("tldr") or {}).get("text")
            if tldr:
                return tldr, "s2_tldr"
            return None, None
        except Exception:
            logger.debug("S2 조회 실패 doi=%s", doi, exc_info=True)
            return None, None
    return None, None


# ---------------------------------------------------------------------------
# 경로별 처리
# ---------------------------------------------------------------------------

async def _enrich_one(
    ref: Ref,
    clients: dict[str, httpx.AsyncClient],
    sems: dict[str, asyncio.Semaphore],
    limiters: dict[str, "_RateLimiter"],
) -> dict[str, Any]:
    """한 건을 채운다. 반환 dict는 그대로 UPDATE에 쓰인다."""
    result: dict[str, Any] = {"enrich_status": "no_match"}

    if ref.path == "art":
        async with sems["kci"]:
            detail = await _fetch_kci(clients["kci"], ref.external_id)
        if not detail:
            return {"enrich_status": "error"}
        result = {
            "title_en": detail.get("title_en"),
            "abstract": detail.get("abstract"),
            "abstract_lang": detail.get("abstract_lang"),
            "abstract_source": "kci" if detail.get("abstract") else None,
            "keywords": detail.get("keywords"),
            "citation_count": detail.get("citation_count"),
            "external_url": detail.get("external_url"),
            "publisher": detail.get("publisher"),
            "issn": detail.get("issn"),
            "kci_registered": detail.get("kci_registered"),
            "resolved_doi": _normalize_doi(detail.get("doi")),
            "enrich_status": "ok" if detail.get("abstract") else "no_abstract",
        }
        return result

    # doi 경로는 저장된 DOI를, match 경로는 Crossref로 찾아낸 DOI를 쓴다.
    doi = _normalize_doi(ref.doi)
    resolved_doi = None
    crossref_type: Optional[str] = None
    published_at: Optional[str] = None
    if ref.path == "match":
        async with sems["crossref"]:
            matched = await _crossref_match(clients["crossref"], limiters["crossref"], ref)
        if not matched:
            return {"enrich_status": "no_match"}
        doi, crossref_type, published_at = matched
        resolved_doi = doi

    async with sems["openalex"]:
        work = await _openalex_by(clients["openalex"], doi=doi)

    result = dict(work or {})
    result["resolved_doi"] = resolved_doi
    # 유형은 Crossref가 우선이고 OpenAlex는 폴백이다 — 어휘가 서로 달라서
    # ('journal-article' vs 'article') 섞으면 집계할 때 같은 것이 둘로 갈린다.
    result["paper_type"] = crossref_type or result.pop("paper_type_openalex", None)
    result.pop("paper_type_openalex", None)
    # 발행일은 Crossref에서만 온다. doi 경로(ref.path == "doi")는 Crossref를 부르지
    # 않으므로 여기서는 비고, --backfill-dates가 DOI 단건조회로 채운다.
    result["published_at"] = published_at
    # OpenAlex를 못 찾았어도 DOI가 있으면 최소한 doi.org 링크는 준다.
    if not result.get("external_url"):
        result["external_url"] = f"https://doi.org/{doi}"

    if not result.get("abstract"):
        abstract, source = await _s2_fallback(clients["s2"], limiters["s2"], doi)
        if abstract:
            result["abstract"] = abstract
            result["abstract_source"] = source

    abstract = result.get("abstract")
    result["abstract_lang"] = ("ko" if _is_korean(abstract) else "en") if abstract else None
    result["enrich_status"] = "ok" if abstract else "no_abstract"
    return result


# ---------------------------------------------------------------------------
# DB 입출력
# ---------------------------------------------------------------------------

_SELECT_PENDING = """
    SELECT DISTINCT ON (external_id)
           external_id,
           title,
           journal,
           pubyear,
           authors[1] AS first_author,
           doi
    FROM paper_citation_external_refs
    WHERE {where}
    ORDER BY external_id
"""

_UPDATE = """
    UPDATE paper_citation_external_refs SET
        abstract        = :abstract,
        abstract_lang   = :abstract_lang,
        abstract_source = :abstract_source,
        title_en        = :title_en,
        keywords        = :keywords,
        resolved_doi    = :resolved_doi,
        external_url    = :external_url,
        pdf_url         = :pdf_url,
        citation_count  = :citation_count,
        publisher       = :publisher,
        issn            = :issn,
        is_open_access  = :is_open_access,
        kci_registered  = :kci_registered,
        paper_type      = :paper_type,
        published_at    = :published_at,
        enrich_status   = :enrich_status,
        enriched_at     = :enriched_at
    WHERE external_id = :external_id
"""

_UPDATE_FIELDS = (
    "abstract", "abstract_lang", "abstract_source", "title_en", "keywords", "resolved_doi",
    "external_url", "pdf_url", "citation_count", "publisher", "issn", "is_open_access",
    "kci_registered", "paper_type", "published_at", "enrich_status",
)

# 백필 전용 UPDATE. **두 칸만 건드린다** — 초록·키워드·enrich_status는 이미 채워진
# 값이라 다시 쓰면 안 된다(재수집 없이 도는 모드라 덮어쓰면 null로 날아간다).
_UPDATE_DATES = """
    UPDATE paper_citation_external_refs SET
        paper_type   = coalesce(:paper_type, paper_type),
        published_at = coalesce(:published_at, published_at)
    WHERE external_id = :external_id
"""


async def _load_pending(retry_failed: bool, path: Optional[str], limit: Optional[int]) -> list[Ref]:
    clauses = ["enrich_status IS NULL"] if not retry_failed else ["(enrich_status IS NULL OR enrich_status IN ('error','no_match'))"]
    if path == "art":
        clauses.append("external_id LIKE 'ART%'")
    elif path == "doi":
        clauses.append("external_id NOT LIKE 'ART%' AND doi IS NOT NULL AND doi <> ''")
    elif path == "match":
        clauses.append("external_id NOT LIKE 'ART%' AND (doi IS NULL OR doi = '')")

    query = _SELECT_PENDING.format(where=" AND ".join(clauses))
    if limit:
        query += f" LIMIT {int(limit)}"

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(query))).mappings().all()
    return [
        Ref(
            external_id=r["external_id"], title=r["title"], journal=r["journal"],
            pubyear=r["pubyear"], first_author=r["first_author"], doi=r["doi"],
        )
        for r in rows
    ]


async def _write(external_id: str, result: dict[str, Any]) -> None:
    params = {field: result.get(field) for field in _UPDATE_FIELDS}
    params["external_id"] = external_id
    params["enriched_at"] = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await db.execute(text(_UPDATE), params)
        await db.commit()


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

# 백필 대상 — 이미 enrich를 마쳤고 DOI를 아는데 발행일이 비어 있는 행.
# enrich_status가 no_match인 행은 DOI 자체가 없어 Crossref를 칠 방법이 없다.
# 그런 행은 published_at을 null로 둔다 — pubyear를 옮겨 담으면 출처가 다른 값이
# 한 칸에 섞여 나중에 구분할 수 없게 된다.
_SELECT_BACKFILL = """
    SELECT DISTINCT ON (external_id)
           external_id,
           coalesce(resolved_doi, doi) AS doi
    FROM paper_citation_external_refs
    WHERE enrich_status IS NOT NULL
      AND coalesce(resolved_doi, doi) IS NOT NULL
      AND published_at IS NULL
    ORDER BY external_id
"""


async def run_backfill(args: argparse.Namespace) -> None:
    """이미 적재된 행에 발행일·논문유형만 채운다.

    초록을 다시 받지 않는다 — DOI를 이미 아니 Crossref 단건조회 한 번이면 끝이고,
    쓰기도 두 칸만 한다(_UPDATE_DATES). enrich_status는 건드리지 않으므로
    이 모드를 몇 번 돌려도 기존 적재 결과가 바뀌지 않는다.
    """
    if not settings.openalex_mailto:
        raise SystemExit("OPENALEX_MAILTO가 설정돼 있지 않습니다 (Crossref polite pool 식별자).")

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(_SELECT_BACKFILL))).all()
    targets = [(r.external_id, r.doi) for r in rows]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        logger.info("백필할 행이 없습니다.")
        return

    logger.info("백필 대상 %d건 (Crossref DOI 단건조회)", len(targets))
    if args.dry_run:
        for external_id, doi in targets[:10]:
            logger.info("  %s | %s", external_id, doi)
        logger.info("dry-run이라 외부 호출/쓰기를 하지 않았습니다.")
        return

    limiter = _RateLimiter(RATE_PER_SEC["crossref"])
    sem = asyncio.Semaphore(CONCURRENCY["crossref"])
    counts = {"날짜O": 0, "날짜X": 0, "조회실패": 0}
    precision: dict[int, int] = {}
    started = time.monotonic()
    done = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers=_user_agent(), follow_redirects=True) as client:

        async def worker(external_id: str, doi: str) -> None:
            nonlocal done
            async with sem:
                got = await _crossref_by_doi(client, limiter, doi)
            if got is None:
                counts["조회실패"] += 1
            else:
                paper_type, published_at = got
                if published_at:
                    counts["날짜O"] += 1
                    depth = published_at.count("-") + 1
                    precision[depth] = precision.get(depth, 0) + 1
                else:
                    counts["날짜X"] += 1
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        text(_UPDATE_DATES),
                        {"external_id": external_id, "paper_type": paper_type, "published_at": published_at},
                    )
                    await db.commit()
            done += 1
            if done % 200 == 0:
                rate = done / max(time.monotonic() - started, 1e-9)
                left = (len(targets) - done) / max(rate, 1e-9) / 60
                logger.info(
                    "  %d/%d (%.1f%%) %s | %.1f건/초 | 남은 시간 약 %.0f분",
                    done, len(targets), 100 * done / len(targets), counts, rate, left,
                )

        await asyncio.gather(*(worker(eid, doi) for eid, doi in targets))

    lbl = {1: "연도만", 2: "연-월", 3: "연-월-일"}
    logger.info("완료: %s / %.1f분", counts, (time.monotonic() - started) / 60)
    for depth in sorted(precision):
        logger.info("  %-8s %d건 (%.1f%%)", lbl.get(depth, depth), precision[depth],
                    100 * precision[depth] / max(counts["날짜O"], 1))


async def run(args: argparse.Namespace) -> None:
    # mailto가 없으면 Crossref/OpenAlex 익명 풀로 떨어진다(Crossref 기준 3 req/s → 1 req/s,
    # 동시 3 → 1). 그 상태로 대량 실행하면 전량 429가 되고, 실패한 행이 no_match로
    # 기록돼 재시도 대상에서 빠지기까지 한다. 시작 전에 막는다.
    if not settings.openalex_mailto:
        raise SystemExit(
            "OPENALEX_MAILTO가 설정돼 있지 않습니다.\n"
            "  Crossref/OpenAlex polite pool 식별자로 필요합니다. 없으면 익명 풀(1 req/s)로\n"
            "  제한돼 대량 조회가 전부 429로 실패합니다. .env에 아래를 추가하세요:\n"
            "    OPENALEX_MAILTO=your@email.com"
        )

    refs = await _load_pending(args.retry_failed, args.path, args.limit)
    if not refs:
        logger.info("처리할 행이 없습니다 (이미 전부 적재됨).")
        return

    buckets: dict[str, int] = {}
    for ref in refs:
        buckets[ref.path] = buckets.get(ref.path, 0) + 1
    logger.info("대상 %d건 — %s", len(refs), ", ".join(f"{k}:{v}" for k, v in sorted(buckets.items())))

    if args.dry_run:
        for ref in refs[:10]:
            logger.info("  [%s] %s | %s", ref.path, ref.external_id, (ref.title or "")[:70])
        logger.info("dry-run이라 외부 호출/쓰기를 하지 않았습니다.")
        return

    stats = Stats(total=len(refs))
    sems = {name: asyncio.Semaphore(n) for name, n in CONCURRENCY.items()}
    limiters = {name: _RateLimiter(rate) for name, rate in RATE_PER_SEC.items()}
    timeout = httpx.Timeout(30.0)
    started = time.monotonic()

    async with httpx.AsyncClient(timeout=timeout, headers=_user_agent(), follow_redirects=True) as shared:
        clients = {"kci": shared, "openalex": shared, "crossref": shared, "s2": shared}

        async def worker(ref: Ref) -> None:
            try:
                result = await _enrich_one(ref, clients, sems, limiters)
            except Exception:
                logger.warning("처리 실패 %s", ref.external_id, exc_info=True)
                result = {"enrich_status": "error"}
            await _write(ref.external_id, result)

            status = result.get("enrich_status")
            if status == "ok":
                stats.ok += 1
                stats.bump(result.get("abstract_source"))
            elif status == "no_abstract":
                stats.no_abstract += 1
            elif status == "no_match":
                stats.no_match += 1
            else:
                stats.error += 1

            done = stats.ok + stats.no_abstract + stats.no_match + stats.error
            if done % 100 == 0 or done == stats.total:
                elapsed = time.monotonic() - started
                rate = done / elapsed if elapsed else 0
                remain = (stats.total - done) / rate if rate else 0
                logger.info(
                    "%d/%d  초록 %d · 초록없음 %d · 매칭실패 %d · 오류 %d  (%.1f건/s, 남은시간 %.0f분)",
                    done, stats.total, stats.ok, stats.no_abstract, stats.no_match, stats.error,
                    rate, remain / 60,
                )

        # 경로별 동시성은 세마포어가 잡으므로 전체는 넉넉히 풀어둔다.
        await asyncio.gather(*(worker(ref) for ref in refs))

    elapsed = time.monotonic() - started
    logger.info("=" * 60)
    logger.info("완료 %d건 / %.1f분", stats.total, elapsed / 60)
    logger.info("  초록 확보    %5d  (%.1f%%)", stats.ok, 100 * stats.ok / stats.total)
    logger.info("  초록 없음    %5d  (%.1f%%)", stats.no_abstract, 100 * stats.no_abstract / stats.total)
    logger.info("  매칭 실패    %5d  (%.1f%%)", stats.no_match, 100 * stats.no_match / stats.total)
    logger.info("  오류         %5d  (%.1f%%)", stats.error, 100 * stats.error / stats.total)
    logger.info("  출처별: %s", ", ".join(f"{k} {v}" for k, v in sorted(stats.by_source.items())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="대상만 세어보고 외부 호출/쓰기는 하지 않음")
    parser.add_argument("--limit", type=int, default=None, help="처리 건수 상한 (테스트용)")
    parser.add_argument("--path", choices=["art", "doi", "match"], default=None, help="특정 경로만 처리")
    parser.add_argument("--retry-failed", action="store_true", help="error/no_match 건도 다시 시도")
    parser.add_argument(
        "--backfill-dates",
        action="store_true",
        help="이미 적재된 행에 발행일·논문유형만 채운다 (초록·enrich_status는 건드리지 않음)",
    )
    args = parser.parse_args()
    asyncio.run(run_backfill(args) if args.backfill_dates else run(args))


if __name__ == "__main__":
    main()
