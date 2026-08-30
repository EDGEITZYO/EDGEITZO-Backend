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
                "open_access,publication_year,doi,cited_by_count,keywords"
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
    }


async def _crossref_match(
    client: httpx.AsyncClient, limiter: "_RateLimiter", ref: Ref
) -> Optional[str]:
    """제목+저널+연도+저자를 한 문자열로 넘겨 DOI를 역으로 찾는다.
    채택 여부는 Crossref가 주는 score가 아니라 제목 유사도로 판정한다."""
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
                params={"query.bibliographic": query, "rows": 3, "select": "DOI,title"},
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

    best_doi, best_score = None, 0.0
    for item in items:
        candidate = (item.get("title") or [""])[0]
        score = _title_similarity(ref.title, candidate)
        if score > best_score:
            best_score, best_doi = score, item.get("DOI")
    if best_doi and best_score >= TITLE_SIMILARITY_THRESHOLD:
        return _normalize_doi(best_doi)
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
    if ref.path == "match":
        async with sems["crossref"]:
            doi = await _crossref_match(clients["crossref"], limiters["crossref"], ref)
        if not doi:
            return {"enrich_status": "no_match"}
        resolved_doi = doi

    async with sems["openalex"]:
        work = await _openalex_by(clients["openalex"], doi=doi)

    result = dict(work or {})
    result["resolved_doi"] = resolved_doi
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
        enrich_status   = :enrich_status,
        enriched_at     = :enriched_at
    WHERE external_id = :external_id
"""

_UPDATE_FIELDS = (
    "abstract", "abstract_lang", "abstract_source", "title_en", "keywords", "resolved_doi",
    "external_url", "pdf_url", "citation_count", "publisher", "issn", "is_open_access",
    "kci_registered", "enrich_status",
)


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
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
