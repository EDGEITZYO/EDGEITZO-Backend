"""메타 충실도 점수 + 유형 배분으로 최종 ~950건 CN을 추출하는 선별 스크립트.

실행: python scripts/select_papers.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

INPUT_PATH     = _PROJECT_ROOT / "data" / "parsed"  / "scienceon_parsed.json"
_ENRICHED_PATH = _PROJECT_ROOT / "data" / "parsed"  / "scienceon_enriched.json"
OUTPUT_PATH    = _PROJECT_ROOT / "data" / "selected" / "selected_cns.json"

# 유형별 선발 목표 건수
QUOTAS: dict[str, int] = {
    "JAKO": 647,
    "DIKO": 150,
    "JAFO": 150,
    "CFKO": 3,
}


# ── 메타 충실도 점수 ────────────────────────────────────────────
def _score(paper: dict) -> int:
    s = 0
    if paper.get("DOI"):                          s += 2  # DOI 있음
    if paper.get("ISSN"):                         s += 1  # ISSN 있음
    kw = paper.get("Keyword") or []
    if isinstance(kw, list) and len(kw) >= 3:    s += 1  # 키워드 3개 이상
    author = paper.get("Author") or []
    if author:                                    s += 1  # 저자 있음
    if paper.get("references"):                   s += 1  # 참고문헌 있음
    return s


# ── 필수 조건 통과 여부 ─────────────────────────────────────────
def _is_valid(paper: dict) -> bool:
    if not paper.get("Abstract"):
        return False
    # CFKO는 Keyword 없이 수집되는 경우가 많아 Abstract만 필수
    if paper.get("DBCode") == "CFKO":
        return True
    kw = paper.get("Keyword") or []
    return bool(kw)  # Keyword 1개 이상


# ── 유형별 선별 ────────────────────────────────────────────────
def _select(papers: list[dict], quota: int, dbcode: str) -> list[dict]:
    pool = [p for p in papers if p.get("DBCode") == dbcode and _is_valid(p)]

    # 정렬: 점수 내림차순 → 발행년 내림차순
    pool.sort(key=lambda p: (_score(p), p.get("Pubyear") or 0), reverse=True)

    return pool[:quota]


# ── 통계 산출 ──────────────────────────────────────────────────
def _stats(selected: list[dict]) -> dict:
    if not selected:
        return {}
    scores  = [_score(p) for p in selected]
    years   = [p.get("Pubyear") for p in selected if p.get("Pubyear")]
    yr_dist = dict(sorted(Counter(years).items()))
    return {
        "평균_점수": round(sum(scores) / len(scores), 2),
        "최저_점수": min(scores),
        "최고_점수": max(scores),
        "발행년_분포": yr_dist,
    }


# ── 메인 ───────────────────────────────────────────────────────
def main() -> None:
    if not INPUT_PATH.exists():
        print(f"[오류] 입력 파일 없음: {INPUT_PATH}")
        print("  → parse_papers.py를 먼저 실행하세요.")
        sys.exit(1)

    raw_data = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    papers   = raw_data.get("papers", [])
    print(f"입력: {len(papers)}건 로드 ({INPUT_PATH.name})")

    # enriched 파일이 있으면 참고문헌 정보를 모수에 주입 (references 가점 반영)
    if _ENRICHED_PATH.exists():
        enriched_refs = {
            p["CN"]: p.get("references", [])
            for p in json.loads(_ENRICHED_PATH.read_text(encoding="utf-8")).get("papers", [])
            if p.get("CN")
        }
        injected = 0
        for p in papers:
            if p.get("CN") in enriched_refs:
                p["references"] = enriched_refs[p["CN"]]
                injected += 1
        print(f"  → enriched {injected}건 참고문헌 주입 완료 (references +1 가점 적용)")

    # 모수 현황
    db_pool = Counter(p.get("DBCode") for p in papers if _is_valid(p))
    print("\n[모수 현황 — 필수 조건 통과]")
    for db, quota in QUOTAS.items():
        avail = db_pool.get(db, 0)
        flag  = "✓" if avail >= quota else f"✗ 부족 ({avail}건)"
        print(f"  {db}: {avail}건 가용 / 목표 {quota}건 {flag}")

    # 유형별 선별
    all_selected: list[dict] = []
    result_counts: dict[str, int] = {}
    stats_by_db: dict[str, dict] = {}

    for dbcode, quota in QUOTAS.items():
        selected = _select(papers, quota, dbcode)
        all_selected.extend(selected)
        result_counts[dbcode] = len(selected)
        stats_by_db[dbcode]   = _stats(selected)
        print(f"\n[{dbcode}] {len(selected)}건 선별 (목표 {quota}건)")
        if selected:
            yr = stats_by_db[dbcode]["발행년_분포"]
            print(f"  점수: {stats_by_db[dbcode]['최저_점수']}~{stats_by_db[dbcode]['최고_점수']}"
                  f"  평균 {stats_by_db[dbcode]['평균_점수']}")
            print(f"  연도: {yr}")

    cns = [p["CN"] for p in all_selected]
    total = len(cns)

    output = {
        "선별일시":  datetime.now().isoformat(),
        "총_선별":   total,
        "유형별":    result_counts,
        "통계":      stats_by_db,
        "cns":       cns,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n=== 선별 완료: {total}건 → {OUTPUT_PATH} ===")
    print(f"  유형별: { {k: v for k, v in result_counts.items()} }")


if __name__ == "__main__":
    main()
