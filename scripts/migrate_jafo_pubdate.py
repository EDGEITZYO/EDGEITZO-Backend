"""JAFO(해외 학술지) pubdate 결측 백필.

원인: scripts/load_papers_postgres.py의 _fmt_pubdate()가 원래 8자리(YYYYMMDD)
형식만 처리했다. 원본 JSON엔 6자리(YYYYMM)·4자리(YYYY) Pubdate도 섞여 있는데
전부 None으로 버려졌다 (JAFO 150건 중 61건). _fmt_pubdate() 수정 후,
이미 적재된 기존 행을 이 스크립트로 백필한다.

DIKO(학위논문)는 원본 자체에 Pubdate가 없는 구조적 결측이라 대상에서 제외.

사용법:
  python scripts/migrate_jafo_pubdate.py           # dry-run
  python scripts/migrate_jafo_pubdate.py --apply
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
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k:
            os.environ.setdefault(_k, _v)

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from scripts.load_papers_postgres import _fmt_pubdate, _DEFAULT_JSON


async def main(apply: bool) -> None:
    data = json.loads(_DEFAULT_JSON.read_text(encoding="utf-8"))
    papers = data.get("papers", data) if isinstance(data, dict) else data
    jafo_raw = {p["CN"]: p.get("Pubdate") for p in papers if p.get("DBCode") == "JAFO" and p.get("CN")}

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT id FROM papers WHERE db_code = 'JAFO' AND pubdate IS NULL")
            )
        ).fetchall()
        missing_ids = [r[0] for r in rows]

        by_len: dict[int, int] = {}
        to_update: list[tuple[str, str]] = []
        still_missing = 0
        not_in_source = 0

        for paper_id in missing_ids:
            raw = jafo_raw.get(paper_id)
            if raw is None:
                not_in_source += 1
                continue
            raw_len = len(str(raw).strip())
            by_len[raw_len] = by_len.get(raw_len, 0) + 1

            new_val = _fmt_pubdate(raw)
            if new_val:
                to_update.append((paper_id, new_val))
            else:
                still_missing += 1

        print(f"=== JAFO pubdate 결측 {len(missing_ids)}건 분석 ===")
        print(f"원본 Pubdate 자리수 분포: {dict(sorted(by_len.items()))}")
        print(f"새로 채워질 건수: {len(to_update)}")
        print(f"여전히 결측(4자리/원본도 없음): {still_missing + not_in_source}")
        if to_update:
            print("\n샘플 5건:")
            for paper_id, new_val in to_update[:5]:
                print(f"  {paper_id} -> {new_val}")

        if not apply:
            print("\n[dry-run] DB 쓰기 생략. --apply로 실행하세요.")
            return

        for paper_id, new_val in to_update:
            await session.execute(
                text("UPDATE papers SET pubdate = :pubdate, updated_at = now() WHERE id = :id"),
                {"pubdate": new_val, "id": paper_id},
            )
        await session.commit()
        print(f"\n[완료] {len(to_update)}건 업데이트됨")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JAFO pubdate 결측 백필")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
