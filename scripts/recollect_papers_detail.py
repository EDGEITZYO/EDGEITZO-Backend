"""ScienceON 상세 API로 1,200건 CN 재수집 → data/parsed/paper_details.json 저장.

결과 형식:
{
  "meta": {"total": 1200, "success": N, "failed": M, "collected_at": "..."},
  "details": {
    "CN001": {
      "references": [{"cited_cn": ..., "title": ..., ...}, ...],
      "similar": [{"similar_cn": ..., "title": ..., ...}, ...]
    },
    ...
  }
}
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))

import httpx

from app.integrations.scienceon.detail_client import PaperDetail, fetch_paper_detail

RAW_PATH = _PROJECT_ROOT / "data" / "raw" / "scienceon_raw.json"
OUTPUT_PATH = _PROJECT_ROOT / "data" / "parsed" / "paper_details.json"

CONCURRENCY = 8
SLEEP_BETWEEN = 0.15   # 초
MAX_RETRIES = 3


async def _fetch_with_retry(cn: str, http: httpx.AsyncClient) -> PaperDetail:
    for attempt in range(MAX_RETRIES):
        detail = await fetch_paper_detail(cn, client=http)
        # references=0, similar=0 모두 비어있으면 실패로 간주 (API 오류 가능성)
        if detail.references or detail.similar:
            return detail
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(1.0 * (attempt + 1))
    return detail


async def main() -> None:
    if not RAW_PATH.exists():
        print(f"[오류] {RAW_PATH} 없음")
        sys.exit(1)

    data = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    cns: list[str] = [p["CN"] for p in data["papers"] if p.get("CN")]
    total = len(cns)
    print(f"=== ScienceON 상세 API 재수집 시작: {total}건 ===")
    print(f"    concurrency={CONCURRENCY}, sleep={SLEEP_BETWEEN}s/req, retry={MAX_RETRIES}")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, dict] = {}
    success = 0
    failed = 0
    start = time.time()

    async def _worker(cn: str, http: httpx.AsyncClient, idx: int) -> None:
        nonlocal success, failed
        async with semaphore:
            await asyncio.sleep(SLEEP_BETWEEN * (idx % CONCURRENCY))
            detail = await _fetch_with_retry(cn, http)
            results[cn] = {
                "references": [
                    {
                        "cited_cn": r.cited_cn,
                        "title": r.title,
                        "author": r.author,
                        "journal": r.journal,
                        "doi": r.doi,
                        "pubyear": r.pubyear,
                        "issn": r.issn,
                        "material_type": r.material_type,
                    }
                    for r in detail.references
                ],
                "similar": [
                    {
                        "similar_cn": s.similar_cn,
                        "title": s.title,
                        "author": s.author,
                        "pubyear": s.pubyear,
                        "issn": s.issn,
                        "material_type": s.material_type,
                    }
                    for s in detail.similar
                ],
            }

    async with httpx.AsyncClient(timeout=20.0) as http:
        tasks = [_worker(cn, http, i) for i, cn in enumerate(cns)]
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 100 == 0 or done == total:
                elapsed = time.time() - start
                print(f"  진행: {done}/{total}건 ({elapsed:.0f}초 경과)")

    for cn, detail in results.items():
        if detail["references"] or detail["similar"]:
            success += 1
        else:
            failed += 1

    output = {
        "meta": {
            "collected_at": datetime.now().isoformat(),
            "total": total,
            "success": success,
            "failed": failed,
        },
        "details": results,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    elapsed = time.time() - start
    print(f"\n=== 완료 ===")
    print(f"총: {total}건 | 성공(데이터 있음): {success}건 | 빈 응답: {failed}건")
    print(f"소요: {elapsed:.0f}초 | 저장: {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
