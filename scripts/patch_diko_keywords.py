"""DIKO 학위논문 키워드만 패치하는 스크립트.

load_diko_thesis_meta.py 실행 후 raw JSON이 업데이트된 상태에서 실행.

동작:
  1. scienceon_raw.json에서 DIKO 논문만 추출
  2. parse_paper() → normalize_paper_keywords() 적용
  3. scienceon_keywords_normalized.json의 DIKO 항목만 교체 (나머지 유지)

사용법:
  python scripts/patch_diko_keywords.py
  python scripts/patch_diko_keywords.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.parse_papers import parse_paper
from scripts.normalize_keywords import normalize_paper_keywords

RAW_PATH        = _PROJECT_ROOT / "data" / "raw"    / "scienceon_raw.json"
NORMALIZED_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="파일 저장 없이 결과만 출력")
    args = parser.parse_args()

    raw_data = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    diko_papers = [
        p for p in raw_data.get("papers", [])
        if str(p.get("DBCode", "")).upper() == "DIKO"
    ]
    print(f"DIKO 논문: {len(diko_papers)}건")

    # parse → normalize
    patched: dict[str, dict] = {}
    for p in diko_papers:
        parsed = parse_paper(p)
        normalized, _ = normalize_paper_keywords(parsed)
        cn = normalized.get("CN")
        if cn:
            patched[cn] = normalized

    print(f"패치 대상: {len(patched)}건")

    # 기존 normalized JSON 로드 후 DIKO만 교체
    normalized_data = json.loads(NORMALIZED_PATH.read_text(encoding="utf-8"))
    papers: list[dict] = normalized_data.get("papers", [])

    updated = 0
    for i, paper in enumerate(papers):
        cn = paper.get("CN", "")
        if cn in patched:
            papers[i] = patched[cn]
            updated += 1

    # 기존에 없던 DIKO 논문 추가
    existing_cns = {p.get("CN") for p in papers}
    added = 0
    for cn, paper in patched.items():
        if cn not in existing_cns:
            papers.append(paper)
            added += 1

    print(f"업데이트: {updated}건 / 신규 추가: {added}건")

    if args.dry_run:
        print("[dry-run] 저장 생략")
        sample = next((p for p in papers if p.get("CN", "").startswith("DIKO") and p.get("Keyword")), None)
        if sample:
            print(f"샘플 CN: {sample['CN']}, 키워드: {sample.get('Keyword', [])[:5]}")
        return

    normalized_data["papers"] = papers
    normalized_data.setdefault("meta", {})
    normalized_data["meta"]["diko_patched_at"] = datetime.now().isoformat()

    NORMALIZED_PATH.write_text(
        json.dumps(normalized_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"저장 완료: {NORMALIZED_PATH.name}")


if __name__ == "__main__":
    main()
