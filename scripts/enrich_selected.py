"""선별된 950건에 대해 browse API로 참고문헌·유사문헌을 보강한다.

- 입력: data/selected/selected_cns.json (950 CN)
        data/parsed/scienceon_parsed.json (기존 파싱 데이터)
- 출력: data/parsed/scienceon_enriched.json (파싱 데이터 + references/similar 병합)
- 체크포인트: data/checkpoints/enrich_checkpoint.json (재시작 시 완료 CN 스킵)

실행: python scripts/enrich_selected.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))

import httpx

from app.integrations.scienceon.detail_client import PaperDetail, fetch_paper_detail

SELECTED_PATH    = _PROJECT_ROOT / "data" / "selected"   / "selected_cns.json"
PARSED_PATH      = _PROJECT_ROOT / "data" / "parsed"     / "scienceon_parsed.json"
OUTPUT_PATH      = _PROJECT_ROOT / "data" / "parsed"     / "scienceon_enriched.json"
CHECKPOINT_PATH  = _PROJECT_ROOT / "data" / "checkpoints" / "enrich_checkpoint.json"

CONCURRENCY  = 8
SLEEP_PER_WORKER = 0.15   # 초 (동시 요청 수 * sleep ≈ API rate limit 여유)
MAX_RETRIES  = 3
SAVE_EVERY   = 50         # N건마다 체크포인트 저장


# ── 체크포인트 ─────────────────────────────────────────────────
def _load_checkpoint() -> dict[str, dict]:
    """완료된 CN → detail dict 로드. 파일 없으면 빈 dict."""
    if CHECKPOINT_PATH.exists():
        data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        return data.get("done", {})
    return {}


def _save_checkpoint(done: dict[str, dict]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(
        json.dumps({"saved_at": datetime.now().isoformat(), "done": done},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── API 호출 (재시도 포함) ─────────────────────────────────────
async def _fetch_with_retry(cn: str, http: httpx.AsyncClient) -> PaperDetail:
    for attempt in range(MAX_RETRIES):
        detail = await fetch_paper_detail(cn, client=http)
        if detail.references or detail.similar:
            return detail
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(1.0 * (attempt + 1))
    return detail


def _detail_to_dict(detail: PaperDetail) -> dict:
    return {
        "references": [
            {
                "cited_cn":      r.cited_cn,
                "title":         r.title,
                "author":        r.author,
                "journal":       r.journal,
                "doi":           r.doi,
                "pubyear":       r.pubyear,
                "issn":          r.issn,
                "material_type": r.material_type,
            }
            for r in detail.references
        ],
        "similar": [
            {
                "similar_cn":    s.similar_cn,
                "title":         s.title,
                "author":        s.author,
                "pubyear":       s.pubyear,
                "issn":          s.issn,
                "material_type": s.material_type,
            }
            for s in detail.similar
        ],
    }


# ── 메인 ───────────────────────────────────────────────────────
async def main() -> None:
    for path in (SELECTED_PATH, PARSED_PATH):
        if not path.exists():
            print(f"[오류] 파일 없음: {path}")
            sys.exit(1)

    cns: list[str] = json.loads(SELECTED_PATH.read_text(encoding="utf-8"))["cns"]
    parsed_list: list[dict] = json.loads(PARSED_PATH.read_text(encoding="utf-8"))["papers"]
    parsed_by_cn: dict[str, dict] = {p["CN"]: p for p in parsed_list if p.get("CN")}

    done: dict[str, dict] = _load_checkpoint()
    todo = [cn for cn in cns if cn not in done]

    total    = len(cns)
    skipped  = total - len(todo)
    print(f"=== browse 보강 시작: 950건 중 {len(todo)}건 미완료 ({skipped}건 체크포인트 스킵) ===")
    print(f"    concurrency={CONCURRENCY}, sleep={SLEEP_PER_WORKER}s, retry={MAX_RETRIES}\n")

    semaphore = asyncio.Semaphore(CONCURRENCY)
    lock      = asyncio.Lock()
    completed = 0
    start     = time.time()

    async def _worker(cn: str, http: httpx.AsyncClient, idx: int) -> None:
        nonlocal completed
        async with semaphore:
            await asyncio.sleep(SLEEP_PER_WORKER * (idx % CONCURRENCY))
            detail = await _fetch_with_retry(cn, http)
            result = _detail_to_dict(detail)

            async with lock:
                done[cn] = result
                completed += 1
                if completed % SAVE_EVERY == 0 or completed == len(todo):
                    _save_checkpoint(done)
                    elapsed = time.time() - start
                    print(f"  진행: {skipped + completed}/{total}건 "
                          f"({elapsed:.0f}초 경과, 체크포인트 저장)")

    async with httpx.AsyncClient(timeout=20.0) as http:
        tasks = [_worker(cn, http, i) for i, cn in enumerate(todo)]
        await asyncio.gather(*tasks)

    # ── 병합 ──────────────────────────────────────────────────
    enriched_papers: list[dict] = []
    for cn in cns:
        base   = parsed_by_cn.get(cn, {"CN": cn})
        detail = done.get(cn, {"references": [], "similar": []})
        enriched_papers.append({**base, **detail})

    # ── 통계 ──────────────────────────────────────────────────
    db_ref_stats: dict[str, dict] = {}
    for dbcode in ("JAKO", "DIKO", "JAFO", "CFKO"):
        group = [p for p in enriched_papers if p.get("DBCode") == dbcode]
        if not group:
            continue
        has_refs = sum(1 for p in group if p.get("references"))
        avg_refs = sum(len(p.get("references", [])) for p in group) / len(group)
        db_ref_stats[dbcode] = {
            "총건수":          len(group),
            "참고문헌_있음":   has_refs,
            "참고문헌_없음":   len(group) - has_refs,
            "참고문헌_보유율": f"{has_refs / len(group) * 100:.1f}%",
            "평균_참고문헌수": round(avg_refs, 1),
        }

    output = {
        "meta": {
            "enriched_at": datetime.now().isoformat(),
            "total_count": len(enriched_papers),
            "ref_stats":   db_ref_stats,
        },
        "papers": enriched_papers,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = time.time() - start
    print(f"\n=== 완료: {len(enriched_papers)}건 → {OUTPUT_PATH} ({elapsed:.0f}초) ===")
    print("\n[유형별 참고문헌 보유율]")
    for db, s in db_ref_stats.items():
        print(f"  {db}: {s['참고문헌_있음']}/{s['총건수']}건 "
              f"({s['참고문헌_보유율']})  평균 {s['평균_참고문헌수']}건/논문")


if __name__ == "__main__":
    asyncio.run(main())
