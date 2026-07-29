"""papers.journal_name 백필.

papers.journal_id(SJR journals 테이블 FK)는 KCI 논문 950건 전량 매칭 실패 상태라
상세 API가 journal_name을 항상 null로 내려주는 문제가 있었음. ScienceON이 준 원본
JournalName은 로컬 JSON(data/parsed/scienceon_enriched.json)에 이미 다 있으므로
재수집 없이 이 값으로 papers.journal_name(신규 컬럼)을 채운다.

사용법:
  python scripts/backfill_paper_journal_name.py --dry-run
  python scripts/backfill_paper_journal_name.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from sqlalchemy import text as sql_text

from app.core.database import AsyncSessionLocal

JSON_PATH = PROJECT_ROOT / "data/parsed/scienceon_enriched.json"


async def run(*, dry_run: bool) -> None:
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    cn_to_name = {
        p["CN"]: p["JournalName"].strip()
        for p in data["papers"]
        if p.get("CN") and p.get("JournalName")
    }
    print(f"로컬 JSON에서 JournalName 보유: {len(cn_to_name)}건")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sql_text("SELECT scienceon_cn, journal_name FROM papers WHERE scienceon_cn IS NOT NULL")
        )
        rows = result.fetchall()

        updates: list[tuple[str, str]] = []
        for cn, current in rows:
            new_name = cn_to_name.get(cn)
            if new_name and new_name != current:
                updates.append((cn, new_name))

        print(f"전체 대상: {len(rows)}건 / 백필 대상: {len(updates)}건")

        if dry_run:
            print("\n[dry-run] DB 쓰기 생략. 예시 3건:")
            for cn, name in updates[:3]:
                print(f"  CN={cn} -> journal_name={name!r}")
            return

        updated = 0
        for cn, name in updates:
            r = await session.execute(
                sql_text("UPDATE papers SET journal_name = :name WHERE scienceon_cn = :cn"),
                {"name": name, "cn": cn},
            )
            updated += r.rowcount
        await session.commit()
        print(f"DB 업데이트: {updated}건")


def main() -> None:
    parser = argparse.ArgumentParser(description="papers.journal_name 백필")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("[dry-run] DB 쓰기 생략\n")
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
