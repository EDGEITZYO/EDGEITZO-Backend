"""DIKO 학위논문 degree 백필 — ScienceON API 재호출 없이 로컬 캐시에서 복구.

배경: load_diko_thesis_meta.py가 과거(2026-05-26)에 이미 degree를 조회해
data/checkpoints/diko_thesis_meta.json에 저장했지만, 그 이후(2026-06-02) papers
테이블이 통째로 재적재되면서 degree 컬럼이 전부 NULL로 리셋됐다. 캐시 파일에
남아있는 값을 다시 Postgres에 반영만 하면 되므로 API 재호출은 필요 없다.

- degree가 이미 채워진 행은 건드리지 않는다 (idempotent, 재실행 안전).
- 체크포인트에 없거나 degree를 못 얻은 CN은 그대로 NULL 유지("학위논문" fallback).

사용법:
  python scripts/backfill_diko_degree.py
  python scripts/backfill_diko_degree.py --dry-run
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

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.paper import Paper

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "diko_thesis_meta.json"


async def process(dry_run: bool) -> None:
    checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    degree_by_cn = {
        cn: v["degree"] for cn, v in checkpoint.items()
        if v.get("status") == "ok" and v.get("degree")
    }
    print(f"[캐시] degree 보유 CN: {len(degree_by_cn)}개")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Paper).where(Paper.db_code == "DIKO"))
        papers = result.scalars().all()
        print(f"[DB] DIKO 논문: {len(papers)}건")

        updated = skipped_has_degree = skipped_no_cache = 0
        for p in papers:
            if p.degree:
                skipped_has_degree += 1
                continue
            degree = degree_by_cn.get(p.scienceon_cn)
            if not degree:
                skipped_no_cache += 1
                continue
            print(f"  {p.scienceon_cn}: degree={degree}")
            if not dry_run:
                p.degree = degree
            updated += 1

        if not dry_run:
            await session.commit()

        print(
            f"\n[완료] 업데이트={updated} 이미채워짐={skipped_has_degree} "
            f"캐시없음={skipped_no_cache}"
        )
        if dry_run:
            print("[dry-run] 실제 커밋 안 함")


def main() -> None:
    parser = argparse.ArgumentParser(description="DIKO degree 백필 (캐시 → Postgres)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(process(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
