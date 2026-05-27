"""
ScienceON 논문을 Postgres papers 테이블에 적재.

사용법:
  python scripts/load_papers_postgres.py
  python scripts/load_papers_postgres.py --dry-run
  python scripts/load_papers_postgres.py --reset
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
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
from app.models.paper import Paper  # noqa: E402

_DEFAULT_JSON = PROJECT_ROOT / "data/parsed/scienceon_keywords_normalized.json"


def _normalize_issn(issn_val: Any) -> str | None:
    """ISSN 리스트에서 첫 번째 값을 하이픈 제거 후 반환."""
    if not issn_val or not isinstance(issn_val, list):
        return None
    cleaned = issn_val[0].replace("-", "").strip()
    return cleaned or None


def _safe_str(val: Any, max_len: int) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s[:max_len] if s else None


def _fmt_pubdate(val: Any) -> str | None:
    """'20250601' → '2025.06.01'. 형식 불일치 시 None."""
    if not val:
        return None
    s = str(val).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}.{s[4:6]}.{s[6:8]}"
    return None


def _safe_list(val: Any) -> list[str] | None:
    if not val or not isinstance(val, list):
        return None
    result = [str(v).strip() for v in val if v]
    return result or None


def build_records(papers: list[dict]) -> list[dict[str, Any]]:
    """JSON 논문 리스트 → DB 레코드 리스트. CN/DOI 중복 제거."""
    now = datetime.now(timezone.utc)
    records: list[dict] = []
    seen_cns: set[str] = set()
    seen_dois: set[str] = set()

    for p in papers:
        cn = (p.get("CN") or "").strip()
        if not cn or cn in seen_cns:
            continue
        seen_cns.add(cn)

        title = _safe_str(p.get("Title"), 1000)
        if not title:
            continue

        doi = _safe_str(p.get("DOI"), 200)
        if doi:
            if doi in seen_dois:
                doi = None  # 중복 DOI는 NULL로 — unique constraint 위반 방지
            else:
                seen_dois.add(doi)

        records.append({
            "id": cn,
            "source_type": "scienceon",
            "scienceon_cn": cn,
            "semantic_scholar_id": None,
            "doi": doi,
            "issn": _normalize_issn(p.get("ISSN")),
            "title": title,
            "title_en": _safe_str(p.get("Title2"), 1000),
            "abstract": p.get("Abstract") or None,
            "abstract_en": p.get("Abstract2") or None,
            "authors": _safe_list(p.get("Author")),
            "keywords_ko": _safe_list(p.get("Keyword")),
            "keywords_en": _safe_list(p.get("Keyword2")),
            "pubyear": p.get("Pubyear") if isinstance(p.get("Pubyear"), int) else None,
            "pubdate": _fmt_pubdate(p.get("Pubdate")),
            "paper_type": None,
            "citation_count": 0,
            "journal_id": None,
            "db_code": _safe_str(p.get("DBCode"), 20),
            "source": "knowledge_base",
            "created_at": now,
            "updated_at": now,
        })

    return records


def print_stats(records: list[dict]) -> None:
    total = len(records)
    has_issn = sum(1 for r in records if r["issn"])
    has_doi = sum(1 for r in records if r["doi"])
    db_codes = Counter(r["db_code"] for r in records if r["db_code"])

    print(f"\n[stats]   총 적재 대상: {total:,}건")
    print(f"          ISSN 보유: {has_issn:,}건 ({has_issn / total * 100:.1f}%)")
    print(f"          DOI 보유:  {has_doi:,}건 ({has_doi / total * 100:.1f}%)")
    print(f"          journal_id: 전량 NULL (KCI 저널 — SJR 미매칭)")
    print("          db_code별 건수:")
    for code, cnt in sorted(db_codes.items()):
        print(f"            {code}: {cnt:,}")


async def _reset_papers() -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("DELETE FROM papers"))
        await session.commit()
        return result.rowcount


async def _insert_papers(records: list[dict[str, Any]], chunk_size: int = 500) -> int:
    inserted = 0
    async with AsyncSessionLocal() as session:
        for i in range(0, len(records), chunk_size):
            chunk = records[i: i + chunk_size]
            # PK/unique 충돌 시 전부 스킵 (CN 또는 DOI 중복 방어)
            stmt = pg_insert(Paper).values(chunk).on_conflict_do_nothing()
            await session.execute(stmt)
            await session.commit()
            inserted += len(chunk)
            print(f"  inserted {min(inserted, len(records)):,}/{len(records):,}")
    return inserted


async def _verify() -> None:
    async with AsyncSessionLocal() as session:
        total = (await session.execute(select(func.count()).select_from(Paper))).scalar()
        print(f"\n[verify]  papers COUNT(*): {total:,}")

        has_journal = (
            await session.execute(
                select(func.count()).select_from(Paper).where(Paper.journal_id.isnot(None))
            )
        ).scalar()
        print(f"          journal_id IS NOT NULL: {has_journal:,}")

        src_rows = (
            await session.execute(
                select(Paper.source_type, func.count()).group_by(Paper.source_type)
            )
        ).all()
        print("          source_type 분포:")
        for src, cnt in src_rows:
            print(f"            {src}: {cnt:,}")

        year_rows = (
            await session.execute(
                select(Paper.pubyear, func.count())
                .group_by(Paper.pubyear)
                .order_by(Paper.pubyear)
            )
        ).all()
        print("          pubyear 분포:")
        for yr, cnt in year_rows:
            print(f"            {yr or 'NULL'}: {cnt:,}")

        samples = (await session.execute(select(Paper).limit(3))).scalars().all()
        print("\n          Sample 3 rows:")
        for p in samples:
            print(f"            id={p.id} | title={p.title[:40]} | issn={p.issn} | journal_id={p.journal_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load ScienceON papers into Postgres.")
    parser.add_argument("--json", default=str(_DEFAULT_JSON), help="JSON 파일 경로")
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 없이 통계만")
    parser.add_argument("--reset", action="store_true", help="기존 papers 삭제 후 재적재")
    parser.add_argument("--chunk-size", type=int, default=500, help="Insert 배치 크기")
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"[error] 파일 없음: {json_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[read]    {json_path}")
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    papers = data.get("papers", data) if isinstance(data, dict) else data
    print(f"[parsed]  {len(papers):,}건 로드")

    records = build_records(papers)
    print_stats(records)

    if args.dry_run:
        print("\n[dry-run] DB 쓰기 스킵")
        return

    async def _run() -> None:
        if args.reset:
            deleted = await _reset_papers()
            print(f"[reset]   {deleted:,}건 삭제")

        print(f"\n[insert]  {len(records):,}건 → papers 테이블")
        await _insert_papers(records, chunk_size=args.chunk_size)
        print("[done]    적재 완료")
        await _verify()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
