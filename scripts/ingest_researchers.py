"""papers.authors의 저자명으로 ScienceON 연구자 검색/상세보기 API를 호출해
researchers / researcher_papers 테이블을 채운다.

흐름 (저자명 1개당):
  1. 연구자 검색 API(TI=연구자명)로 동명이인 후보 최대 --candidates명 조회
  2. 각 후보를 연구자 상세보기 API로 조회 → CallAPIInfo에서 그 연구자의 논문 CN 목록 파싱
  3. 그 CN 목록과 우리 papers 테이블의 CN을 교집합 — 겹치는 게 하나도 없으면 동명이인으로 보고 스킵
     (교집합이 있어야만 "우리 논문의 실제 저자"로 확정 — 오매칭 방지)
  4. 확정된 후보만 researchers upsert + 교집합 논문에 대해서만 researcher_papers 연결

사용법:
  python scripts/ingest_researchers.py --dry-run          # API 호출 없이 대상 저자 수만 확인
  python scripts/ingest_researchers.py --limit 20          # 저자 20명만 시범 적재
  python scripts/ingest_researchers.py                     # 전체 적재
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

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
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k:
            os.environ.setdefault(_k, _v)

from app.core.database import AsyncSessionLocal
from app.integrations.scienceon.researcher_client import (
    ScienceOnResearcherClient,
    parse_researcher_detail_xml,
    parse_researcher_search_xml,
)
from app.models.paper import Paper
from app.models.researcher import Researcher, ResearcherPaper

MAX_CANDIDATES_PER_NAME = 3  # 검색 결과 상위 몇 명까지 상세조회로 확인할지
MAX_RETRIES = 5
_PAPER_SEARCH_API_ID = "API-001-01"  # 연구자 상세보기 CallAPIInfo 중 "논문 검색API" 식별값
_CALL_PACING_SECONDS = 0.5  # 호출 사이 간격 — 429(rate limit) 방지용 최소 페이싱
_RATE_LIMIT_BACKOFF_SECONDS = 10  # 429 응답 시 재시도 전 대기(지수 백오프 기준값)


def _split_keyword_field(raw: str | None) -> list[str] | None:
    """연구자 API의 Keyword 필드는 실측 기준 ','로 구분된 문자열
    (논문 API의 Keyword 필드는 '.' 구분 — 서로 다른 포맷이므로 별도 처리)."""
    if not raw:
        return None
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    return terms or None


async def _load_author_names() -> list[str]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT DISTINCT unnest(authors) AS name FROM papers WHERE authors IS NOT NULL")
            )
        ).all()
    return sorted({r.name.strip() for r in rows if r.name and r.name.strip()})


async def _load_known_paper_ids() -> set[str]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(Paper.id))).scalars().all()
    return set(rows)


async def _call_with_retry(coro_factory):
    """호출마다 최소 페이싱을 두고, 429(rate limit)는 일반 오류보다 더 길게 백오프한다."""
    import httpx

    for attempt in range(MAX_RETRIES):
        await asyncio.sleep(_CALL_PACING_SECONDS)
        try:
            return await coro_factory()
        except httpx.HTTPStatusError as exc:
            if attempt == MAX_RETRIES - 1:
                print(f"    [오류] 최종 실패(HTTP {exc.response.status_code}): {exc}")
                return None
            is_rate_limited = exc.response.status_code == 429
            wait = _RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt) if is_rate_limited else 2 ** attempt
            await asyncio.sleep(wait)
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                print(f"    [오류] 최종 실패: {exc}")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


def _extract_paper_cns(call_api_infos) -> set[str]:
    for info in call_api_infos:
        if info.provider_api_id != _PAPER_SEARCH_API_ID:
            continue
        raw = info.parameter_values.get("searchQuery")
        if not raw:
            continue
        try:
            query = json.loads(raw)
        except (ValueError, TypeError):
            continue
        cn_field = query.get("CN", "")
        return {cn.strip() for cn in cn_field.split("|") if cn.strip()}
    return set()


async def _resolve_researcher(
    client: ScienceOnResearcherClient,
    name: str,
    known_paper_ids: set[str],
):
    """이름 1개 → (researcher_record, matched_paper_ids) 또는 (None, set()) (매칭 실패)."""
    search_xml = await _call_with_retry(lambda: client.search_researchers(name, search_field="TI", size=MAX_CANDIDATES_PER_NAME))
    if not search_xml:
        return None, set()

    candidates = [c for c in parse_researcher_search_xml(search_xml) if c.cn]
    for candidate in candidates:
        detail_xml = await _call_with_retry(lambda cn=candidate.cn: client.browse_researcher(cn))
        if not detail_xml:
            continue
        researcher, call_api_infos = parse_researcher_detail_xml(detail_xml)
        if researcher is None or not researcher.cn:
            continue
        candidate_paper_cns = _extract_paper_cns(call_api_infos)
        matched = candidate_paper_cns & known_paper_ids
        if matched:
            return researcher, matched
    return None, set()


async def _upsert_researcher(session, researcher) -> None:
    stmt = pg_insert(Researcher).values(
        cn=researcher.cn,
        author_name_kor=researcher.author_name_kor,
        author_name_eng=researcher.author_name_eng,
        author_inst_kor=researcher.author_inst_kor,
        author_inst_eng=researcher.author_inst_eng,
        rno=researcher.rno,
        email=researcher.email,
        keyword=_split_keyword_field(researcher.keyword),
        article_cnt=researcher.article_cnt,
        patent_cnt=researcher.patent_cnt,
        report_cnt=researcher.report_cnt,
    ).on_conflict_do_update(
        index_elements=["cn"],
        set_={
            "author_name_kor": researcher.author_name_kor,
            "author_name_eng": researcher.author_name_eng,
            "author_inst_kor": researcher.author_inst_kor,
            "author_inst_eng": researcher.author_inst_eng,
            "rno": researcher.rno,
            "email": researcher.email,
            "keyword": _split_keyword_field(researcher.keyword),
            "article_cnt": researcher.article_cnt,
            "patent_cnt": researcher.patent_cnt,
            "report_cnt": researcher.report_cnt,
            "updated_at": sa.func.now(),
        },
    )
    await session.execute(stmt)


async def _link_papers(session, researcher_cn: str, paper_ids: set[str]) -> None:
    if not paper_ids:
        return
    stmt = pg_insert(ResearcherPaper).values(
        [{"researcher_cn": researcher_cn, "paper_id": pid} for pid in paper_ids]
    ).on_conflict_do_nothing()
    await session.execute(stmt)


async def run(*, dry_run: bool, limit: int | None, concurrency: int) -> None:
    print("[준비] papers.authors에서 고유 저자명 수집 중...")
    names = await _load_author_names()
    if limit:
        names = names[:limit]
    print(f"[준비] 대상 저자명 {len(names):,}명")

    if dry_run:
        print("[dry-run] API 호출 없이 종료")
        return

    known_paper_ids = await _load_known_paper_ids()
    print(f"[준비] 서비스 DB papers {len(known_paper_ids):,}건 (CN 매칭 기준)")

    client = ScienceOnResearcherClient()
    semaphore = asyncio.Semaphore(concurrency)
    matched_count = 0
    skipped_count = 0

    async def _process(name: str, idx: int) -> None:
        nonlocal matched_count, skipped_count
        async with semaphore:
            researcher, matched_paper_ids = await _resolve_researcher(client, name, known_paper_ids)
            if researcher is None:
                skipped_count += 1
            else:
                async with AsyncSessionLocal() as session:
                    await _upsert_researcher(session, researcher)
                    await _link_papers(session, researcher.cn, matched_paper_ids)
                    await session.commit()
                matched_count += 1
                print(f"  [{idx}/{len(names)}] '{name}' → CN={researcher.cn} ({len(matched_paper_ids)}건 연결)")

    await asyncio.gather(*(_process(name, i + 1) for i, name in enumerate(names)))

    print(f"\n[완료] 매칭 {matched_count:,}명 / 스킵(동명이인·미검색) {skipped_count:,}명")


def main() -> None:
    parser = argparse.ArgumentParser(description="ScienceON 연구자 데이터 적재")
    parser.add_argument("--dry-run", action="store_true", help="API 호출 없이 대상 저자 수만 확인")
    parser.add_argument("--limit", type=int, default=None, help="처리할 저자명 수 제한 (시범 적재용)")
    parser.add_argument("--concurrency", type=int, default=2, help="동시 처리 저자 수 (기본 2, ScienceON rate limit 고려)")
    args = parser.parse_args()

    from app.core.settings import settings

    if not settings.scienceon_client_id or not settings.scienceon_token:
        print("[오류] SCIENCEON_CLIENT_ID 또는 SCIENCEON_TOKEN이 .env에 설정되지 않았습니다.")
        sys.exit(1)

    asyncio.run(run(dry_run=args.dry_run, limit=args.limit, concurrency=args.concurrency))


if __name__ == "__main__":
    main()
