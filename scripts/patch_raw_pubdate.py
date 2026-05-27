"""raw JSON + papers DB에 Pubdate 추가.

JAKO/JAFO CN별 search API 호출 → Pubdate(YYYYMMDD) 수집
→ data/raw/scienceon_raw.json Pubdate 필드 추가
→ papers.pubdate(YYYY.MM.DD) 업데이트

DIKO: API 미제공 확인됨 → 대상 제외 (raw에 Pubdate 키 없이 유지)

사용법:
  python scripts/patch_raw_pubdate.py
  python scripts/patch_raw_pubdate.py --dry-run
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
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.settings import settings

RAW_PATH = PROJECT_ROOT / "data" / "raw" / "scienceon_raw.json"
DELAY = 0.2
TARGET_DB_CODES = {"JAKO", "JAFO"}


def _fetch_pubdate_raw(cn: str) -> str | None:
    """search API로 CN 조회 → Pubdate 원문(YYYYMMDD) 반환."""
    params = {
        "client_id": settings.scienceon_client_id,
        "token": settings.scienceon_token,
        "version": settings.scienceon_version,
        "action": "search",
        "target": "ARTI",
        "searchQuery": json.dumps({"CN": cn}),
        "rowCount": 1,
        "curPage": 1,
        "include": "CN,Pubdate",
    }
    try:
        resp = httpx.get(settings.scienceon_base_url, params=params, timeout=10)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        flat = {
            item.get("metaCode"): item.text or ""
            for item in root.iter("item")
            if item.get("metaCode")
        }
        val = flat.get("Pubdate", "").strip()
        return val if val and len(val) == 8 and val.isdigit() else None
    except Exception:
        return None


def _fmt(raw: str) -> str:
    return f"{raw[:4]}.{raw[4:6]}.{raw[6:8]}"


async def run(*, dry_run: bool) -> None:
    data = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    papers: list[dict] = data["papers"]

    targets = [(i, p) for i, p in enumerate(papers) if p.get("DBCode") in TARGET_DB_CODES]
    print(f"대상: {len(targets)}건 (JAKO/JAFO) / 전체 {len(papers)}건")

    pubdate_map: dict[str, str] = {}   # CN → YYYYMMDD
    ok = no_date = failed = 0

    for seq, (idx, paper) in enumerate(targets, 1):
        cn = paper.get("CN", "")
        if seq % 50 == 0 or seq == len(targets):
            print(f"  [{seq}/{len(targets)}] ok={ok} no_date={no_date} failed={failed}")

        raw_date = _fetch_pubdate_raw(cn)
        if raw_date:
            pubdate_map[cn] = raw_date
            papers[idx]["Pubdate"] = raw_date
            ok += 1
        else:
            no_date += 1

        time.sleep(DELAY)

    # raw JSON 업데이트
    if not dry_run:
        RAW_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nraw JSON 저장 완료: {RAW_PATH}")

    # DB papers.pubdate 업데이트
    db_updated = 0
    async with AsyncSessionLocal() as session:
        for cn, raw_date in pubdate_map.items():
            if not dry_run:
                result = await session.execute(
                    text("UPDATE papers SET pubdate = :pd WHERE scienceon_cn = :cn AND (pubdate IS NULL OR pubdate <> :pd)"),
                    {"pd": _fmt(raw_date), "cn": cn},
                )
                db_updated += result.rowcount
        if not dry_run:
            await session.commit()

    print(f"\n완료:")
    print(f"  Pubdate 확보:  {ok}건")
    print(f"  Pubdate 없음:  {no_date}건")
    print(f"  오류:          {failed}건")
    print(f"  DB 업데이트:   {db_updated}건")


def main() -> None:
    parser = argparse.ArgumentParser(description="raw JSON + DB pubdate 패치")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("[dry-run] 파일/DB 쓰기 생략\n")
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
