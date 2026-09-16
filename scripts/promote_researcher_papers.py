"""연구자 이력 논문(코퍼스 밖 KCI 논문)을 papers 테이블로 편입한다.

왜 필요한가:
  북마크·읽음 기록·논문 상세페이지는 전부 papers.id를 FK로 본다. 연구자 논문의 93.6%는
  researcher_external_papers에만 있어 papers에 없고, 그래서 연구자 상세페이지에서
  논문을 눌러도 우리 페이지가 없고 북마크 버튼도 동작하지 않는다.

검색에는 영향이 없다:
  검색 코퍼스는 papers 테이블이 아니라 data/parsed/*.json + ChromaDB 'papers' 컬렉션이다.
  이 스크립트는 Postgres papers 행과 journal 연결만 만들고 임베딩·색인은 만들지 않으므로,
  논문 검색·키워드맵·AI 검색 결과는 한 건도 바뀌지 않는다.
  (scripts/add_kci_reference_papers.py가 참고문헌 논문 30편에 쓴 것과 같은 방식이다.)

한 번의 API 호출로 초록까지 같이 받는다:
  external_id가 이미 KCI art_id라 articleDetail을 바로 부를 수 있다. 참고문헌 때처럼
  DOI를 역추적할 필요가 없다. 응답에 초록·ISSN·저자 소속이 함께 들어 있다.

사용법:
  python scripts/promote_researcher_papers.py --dry-run --limit 20
  python scripts/promote_researcher_papers.py --limit 200
  python scripts/promote_researcher_papers.py                 # 전체 (약 25,800편)
  python scripts/promote_researcher_papers.py --concurrency 3 # 기본 2

재실행하면 이미 편입된 논문은 건너뛴다(papers.id 존재 여부로 판단).

실행 결과 (2026-09-16, 전량):
  적재 25,781편 / DOI 연결 16편 / 96.5분 (4.5건/초, 동시성 3)
  초록 96.4% · 학술지 연결 94.2% · 연구자 논문 33,179건 전부 papers와 연결됨
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

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
        if _k.strip():
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import httpx
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.integrations.kci.researcher_client import KCIArticle, KCIResearcherClient

# KCI는 호출 간격을 두지 않으면 429를 낸다. ingest_researchers.py와 같은 값.
CALL_PACING_SECONDS = 0.5
MAX_RETRIES = 3

# 편입 대상: 아직 papers에 없는 외부 논문
_PENDING_SQL = """
SELECT DISTINCT e.external_id
FROM researcher_external_papers e
WHERE e.internal_paper_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM papers p WHERE p.id = e.external_id OR p.kci_art_id = e.external_id)
ORDER BY e.external_id
"""

# 학술지 연결 — ISSN 우선, 없거나 못 찾으면 학술지명(실측 매칭률 91.4%).
# SCI 배지와 신뢰도 점수가 이 연결에 달려 있다. 같은 제목의 저널이 40쌍 있어
# SCI 등재 쪽을 고른다 — 배지를 놓치는 쪽보다 낫다.
_JOURNAL_BY_ISSN_SQL = (
    "SELECT id FROM journals WHERE issn && :forms ORDER BY sci_indexed DESC LIMIT 1"
)
_JOURNAL_BY_NAME_SQL = (
    "SELECT id FROM journals WHERE lower(btrim(title)) = lower(btrim(:journal)) "
    "ORDER BY sci_indexed DESC LIMIT 1"
)

_INSERT_SQL = """
INSERT INTO papers (id, source_type, kci_art_id, title, title_en, abstract, abstract_en,
                    authors, keywords_ko, keywords_en, pubyear, pubdate, paper_type,
                    citation_count, journal_id, journal_name, db_code, source, doi, created_at, updated_at)
VALUES (:id, 'kci', :art_id, :title, :title_en, :abstract, :abstract_en,
        :authors, :keywords, NULL, :pubyear, :pubdate, :paper_type,
        :citation_count, :journal_id, :journal_name, 'JAKO', 'kci', :doi, now(), now())
ON CONFLICT (id) DO NOTHING
"""


def _issn_forms(issn: str | None) -> list[str] | None:
    """journals.issn에는 '1738-3560'과 '17383560'이 섞여 있어 둘 다 시도한다."""
    if not issn:
        return None
    raw = issn.strip()
    forms = {raw, raw.replace("-", "")}
    if "-" not in raw and len(raw) == 8:
        forms.add(f"{raw[:4]}-{raw[4:]}")
    return sorted(forms)


def _pubdate(article: KCIArticle) -> str | None:
    if not article.pubyear:
        return None
    month = (article.pubmonth or "").zfill(2) if article.pubmonth else None
    # KCI는 일자를 주지 않는다. papers.pubdate 규약(있으면 ISO)에 맞춰 1일로 채운다.
    return f"{article.pubyear}-{month}-01" if month and month != "00" else f"{article.pubyear}-01-01"


async def _call_with_retry(coro_factory):
    for attempt in range(MAX_RETRIES):
        await asyncio.sleep(CALL_PACING_SECONDS)
        try:
            return await coro_factory()
        except httpx.HTTPStatusError as exc:
            if attempt == MAX_RETRIES - 1:
                return None
            wait = 10 * (2**attempt) if exc.response.status_code == 429 else 2**attempt
            await asyncio.sleep(wait)
        except (httpx.HTTPError, asyncio.TimeoutError):
            if attempt == MAX_RETRIES - 1:
                return None
            await asyncio.sleep(2**attempt)
    return None


async def _promote_one(session, client: KCIResearcherClient, art_id: str, dry_run: bool) -> str:
    article = await _call_with_retry(lambda: client.article_detail(art_id))
    if article is None:
        return "실패"
    if not article.title:
        return "제목없음"

    journal_id = None
    forms = _issn_forms(article.issn)
    if forms:
        journal_id = (
            await session.execute(text(_JOURNAL_BY_ISSN_SQL), {"forms": forms})
        ).scalar()
    if journal_id is None and article.journal:
        journal_id = (
            await session.execute(text(_JOURNAL_BY_NAME_SQL), {"journal": article.journal})
        ).scalar()

    if dry_run:
        return f"예정 초록{'O' if article.abstract else 'X'} 저널{'O' if journal_id else 'X'}"

    await session.execute(
        text(_INSERT_SQL),
        {
            "id": art_id,
            "art_id": art_id,
            "title": article.title[:1000],
            "title_en": (article.title_eng or None) and article.title_eng[:1000],
            "abstract": article.abstract,
            "abstract_en": article.abstract_eng,
            "authors": [a.name[:300] for a in article.authors] or None,
            "keywords": [k[:200] for k in article.keywords] or None,
            "pubyear": article.pubyear,
            "pubdate": _pubdate(article),
            "paper_type": "학술 저널",
            "citation_count": article.citation_count or 0,
            "journal_id": journal_id,
            "journal_name": (article.journal or None) and article.journal[:500],
            "doi": (article.doi or None) and article.doi[:200],
        },
    )
    # 편입한 논문을 연구자 이력과 연결한다 — 이게 채워져야 API가 상세 이동을 허용한다.
    await session.execute(
        text(
            "UPDATE researcher_external_papers SET internal_paper_id = :pid "
            "WHERE external_id = :pid AND internal_paper_id IS NULL"
        ),
        {"pid": art_id},
    )
    await session.commit()
    return "적재"


async def main() -> None:
    parser = argparse.ArgumentParser(description="연구자 이력 논문을 papers로 편입 (초록 포함)")
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 논문 수")
    parser.add_argument("--concurrency", type=int, default=2, help="동시 호출 수 (기본 2)")
    parser.add_argument("--dry-run", action="store_true", help="적재하지 않고 결과만 확인")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        # 먼저 DOI로 이어붙인다. 같은 논문이 코퍼스에 다른 ID(ScienceON CN)로 이미 있는
        # 경우가 있어, 그대로 INSERT하면 papers.doi 유니크 제약에 걸린다(실측 16건).
        # 새로 넣을 게 아니라 연결하면 되는 건이다.
        linked = (
            await session.execute(
                text(
                    "UPDATE researcher_external_papers e SET internal_paper_id = p.id "
                    "FROM papers p "
                    "WHERE e.internal_paper_id IS NULL AND e.doi IS NOT NULL AND p.doi = e.doi"
                )
            )
        ).rowcount
        if linked:
            await session.commit()
            print(f"DOI가 같은 기존 논문에 연결: {linked}건")

        pending = [r[0] for r in (await session.execute(text(_PENDING_SQL))).all()]
    if args.limit:
        pending = pending[: args.limit]
    if not pending:
        print("편입할 논문이 없습니다.")
        return

    print(f"대상 {len(pending):,}편 / 동시성 {args.concurrency} / {'모의실행' if args.dry_run else '실적재'}")
    eta = len(pending) * CALL_PACING_SECONDS / max(args.concurrency, 1) / 60
    print(f"예상 소요 약 {eta:.0f}분 (KCI 호출 간격 {CALL_PACING_SECONDS}초 기준)\n")

    stats: dict[str, int] = {}
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.time()

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        client = KCIResearcherClient(http_client)

        async def one(idx: int, art_id: str) -> None:
            async with semaphore:
                async with AsyncSessionLocal() as session:
                    try:
                        result = await _promote_one(session, client, art_id, args.dry_run)
                    except Exception as exc:
                        await session.rollback()
                        result = f"오류({type(exc).__name__})"
                key = result.split()[0]
                stats[key] = stats.get(key, 0) + 1
                if idx % 50 == 0 or idx == len(pending):
                    done = sum(stats.values())
                    rate = done / max(time.time() - started, 1)
                    print(
                        f"  {idx:,}/{len(pending):,} ({done/len(pending)*100:.1f}%) "
                        f"{dict(sorted(stats.items()))} | {rate:.1f}건/초"
                    )

        await asyncio.gather(*(one(i, a) for i, a in enumerate(pending, start=1)))

    print(f"\n완료: {dict(sorted(stats.items()))} / {(time.time()-started)/60:.1f}분")


if __name__ == "__main__":
    asyncio.run(main())
