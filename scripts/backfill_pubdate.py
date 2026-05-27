"""papers.pubdate 백필 스크립트.

ScienceON search API에서 JAKO/JAFO CN별 Pubdate(YYYYMMDD) 조회
→ papers.pubdate(YYYY.MM.DD) 저장.

DIKO(학위논문): API에서 Pubdate 미제공 → 대상 제외.

사용법:
  python scripts/backfill_pubdate.py
  python scripts/backfill_pubdate.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
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

import httpx
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.models.paper import Paper

DELAY = 0.2


def _parse_pubdate(raw: str | None) -> str | None:
    if not raw or len(raw) != 8 or not raw.isdigit():
        return None
    return f"{raw[:4]}.{raw[4:6]}.{raw[6:8]}"


def _fetch_pubdate(cn: str) -> str | None:
    params = {
        "client_id": settings.scienceon_client_id,
        "token": settings.scienceon_token,
        "version": settings.scienceon_version,
        "action": "search",
        "target": "ARTI",
        "searchQuery": json.dumps({"CN": cn}),
        "rowCount": 1,
        "curPage": 1,
        "include": "CN,Pubdate,Pubyear",
    }
    try:
        resp = httpx.get(settings.scienceon_base_url, params=params, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        flat = {
            item.get("metaCode"): item.text
            for item in root.iter("item")
            if item.get("metaCode")
        }
        return _parse_pubdate(flat.get("Pubdate"))
    except Exception:
        return None


async def run(*, dry_run: bool) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Paper.id, Paper.scienceon_cn)
            .where(
                Paper.scienceon_cn.isnot(None),
                Paper.db_code.in_(["JAKO", "JAFO"]),
            )
        )
        rows = result.all()

    total = len(rows)
    print(f"대상: {total}건 (JAKO+JAFO, DIKO 제외)")

    updated = no_pubdate = failed = 0

    async with AsyncSessionLocal() as session:
        for i, (paper_id, cn) in enumerate(rows, 1):
            if i % 50 == 0 or i == total:
                print(f"  [{i}/{total}] updated={updated} no_pubdate={no_pubdate} failed={failed}")

            try:
                pubdate = _fetch_pubdate(cn)
                if pubdate:
                    if not dry_run:
                        await session.execute(
                            text("UPDATE papers SET pubdate = :pubdate WHERE id = :id"),
                            {"pubdate": pubdate, "id": paper_id},
                        )
                    updated += 1
                else:
                    no_pubdate += 1
            except Exception:
                failed += 1

            time.sleep(DELAY)

        if not dry_run:
            await session.commit()

    print(f"\n완료:")
    print(f"  pubdate 저장:  {updated}건")
    print(f"  Pubdate 없음:  {no_pubdate}건")
    print(f"  오류:          {failed}건")


def main() -> None:
    parser = argparse.ArgumentParser(description="papers.pubdate 백필 (JAKO+JAFO)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("[dry-run] DB 쓰기 생략\n")
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
