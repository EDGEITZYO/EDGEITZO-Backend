"""ScienceON raw JSON → 정제 파싱 스크립트"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from html import unescape
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.integrations.scienceon.normalizer import _split_dot_text, _split_semicolon_text  # noqa: E402

INPUT_PATH = _PROJECT_ROOT / "data" / "raw" / "scienceon_raw.json"
OUTPUT_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_parsed.json"


# ── 공통 정제 ──────────────────────────────────────────────
# HTML 특수문자 디코딩 후 앞뒤 공백 제거
def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = unescape(str(value)).strip()
    return cleaned or None

# Abstract의 줄바꿈을 공백으로, 연속 공백을 단일 공백으로 정리
def _clean_abstract(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = unescape(str(value))
    cleaned = re.sub(r"[\r\n]+", " ", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned).strip()
    return cleaned or None


# ── 필드별 정제 ────────────────────────────────────────────
# ; 포함이면 세미콜론 split, 없으면 dot split으로 자동 분기해 리스트 반환
def _parse_keyword(value: str | None) -> tuple[list[str], str | None]:
    """(parsed_list, raw_original) 반환. ; 포함이면 semicolon split, 아니면 dot split."""
    raw = value
    if not value:
        return [], None
    if ";" in value:
        return _split_semicolon_text(value), raw
    return _split_dot_text(value), raw

# Keyword2는 구분자 판단 없이 항상 dot split
def _parse_keyword2(value: str | None) -> tuple[list[str], str | None]:
    raw = value
    if not value:
        return [], None
    return _split_dot_text(value), raw

# 세미콜론으로 split해 ISSN 리스트 반환
def _parse_issn(value: str | None) -> list[str] | None:
    if not value:
        return None
    result = _split_semicolon_text(value)
    return result or None

# http:// 또는 https://로 시작하는 값만 유효한 DOI로 처리
def _parse_doi(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.startswith("http://") or stripped.startswith("https://"):
        return stripped
    return None

# 문자열 연도 값을 정수로 변환
def _parse_pubyear(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


# ── 논문 단건 정제 ─────────────────────────────────────────
# 논문 1건의 모든 필드에 정제 함수 적용해 dict 반환
def parse_paper(raw: dict) -> dict:
    kw_list, kw_raw = _parse_keyword(raw.get("Keyword"))
    kw2_list, kw2_raw = _parse_keyword2(raw.get("Keyword2"))

    return {
        "CN":           raw.get("CN"),                          # PK, 변형 금지
        "DBCode":       raw.get("DBCode"),                      # 변형 없음
        "Title":        _clean(raw.get("Title")),
        "Title2":       _clean(raw.get("Title2")),
        "Abstract":     _clean_abstract(raw.get("Abstract")),
        "Abstract2":    _clean_abstract(raw.get("Abstract2")),
        "Keyword":      kw_list if kw_list else None,
        "keyword_raw":  kw_raw,
        "Keyword2":     kw2_list if kw2_list else None,
        "keyword2_raw": kw2_raw,
        "ISSN":         _parse_issn(raw.get("ISSN")),
        "DOI":          _parse_doi(raw.get("DOI")),
        "Pubyear":      _parse_pubyear(raw.get("Pubyear")),
        "JournalName":  _clean(raw.get("JournalName")),
        "Author":       _split_semicolon_text(raw.get("Author")) or None,
    }


# ── 통계 출력 ──────────────────────────────────────────────
# 필드별 null 건수와 비율 통계 출력
def print_stats(papers: list[dict]) -> None:
    total = len(papers)
    check_fields = [
        "Title", "Title2", "Abstract", "Abstract2",
        "Keyword", "Keyword2", "ISSN", "DOI",
        "Pubyear", "JournalName", "Author",
    ]
    print(f"\n{'필드':<14} {'null 건수':>10} {'null 비율':>10}")
    print("-" * 36)
    for field in check_fields:
        null_count = sum(1 for p in papers if not p.get(field))
        print(f"{field:<14} {null_count:>10}건 {null_count / total * 100:>9.1f}%")


# ── 메인 ───────────────────────────────────────────────────
# raw JSON 로드 → 전체 정제 → Abstract null 제거 → 최대 1,000건 저장
def main() -> None:
    if not INPUT_PATH.exists():
        print(f"[오류] 입력 파일 없음: {INPUT_PATH}")
        sys.exit(1)

    raw_data = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    raw_papers = raw_data.get("papers", [])
    print(f"입력: {len(raw_papers)}건 로드 ({INPUT_PATH.name})")

    parsed_papers = [parse_paper(p) for p in raw_papers]

    # Abstract 없는 논문 제거 후 최대 1,000건
    before = len(parsed_papers)
    parsed_papers = [p for p in parsed_papers if p.get("Abstract")][:1000]
    print(f"Abstract 필터: {before}건 → {len(parsed_papers)}건 (제거: {before - len(parsed_papers)}건)")

    output = {
        "meta": {
            "parsed_at": datetime.now().isoformat(),
            "total_count": len(parsed_papers),
            "source": INPUT_PATH.name,
        },
        "papers": parsed_papers,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"출력: {len(parsed_papers)}건 저장 → {OUTPUT_PATH}")

    print_stats(parsed_papers)


if __name__ == "__main__":
    main()
