"""ScienceON 생명공학 논문 수집 스크립트"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# .env 로드를 app import보다 반드시 먼저 실행
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))

from app.integrations.scienceon.client import ScienceOnClient
from app.integrations.scienceon.normalizer import _find_records
from app.integrations.scienceon.parser import parse_scienceon_xml

KEYWORDS = ["생명공학", "바이오", "유전자", "단백질", "세포공학", "줄기세포", "효소", "미생물"]

SAVE_FIELDS = [
    "CN", "DBCode", "Title", "Title2", "Abstract", "Abstract2",
    "Keyword", "Keyword2", "ISSN", "DOI", "Pubyear", "Pubdate", "JournalName", "Author",
]

MAX_TOTAL = 1200
ROW_COUNT = 100
MAX_RETRIES = 3
CURRENT_YEAR = datetime.now().year
OUTPUT_PATH = _PROJECT_ROOT / "data" / "raw" / "scienceon_raw.json"

# API 응답의 [{"@metaCode": "CN", "#text": "..."}] 구조를 {"CN": "..."} flat dict로 변환
def _flatten_record(record: dict) -> dict:
    """item 리스트 구조 → {metaCode: text} flat dict 변환"""
    items = record.get("item", [])
    if isinstance(items, dict):
        items = [items]
    return {
        item["@metaCode"]: item.get("#text")
        for item in items
        if isinstance(item, dict) and item.get("@metaCode")
    }

# flat dict에서 저장할 필드(SAVE_FIELDS)만 골라 추출
def _extract_fields(record: dict) -> dict:
    flat = _flatten_record(record)
    return {field: flat.get(field) for field in SAVE_FIELDS}

# 응답 JSON의 statusCode가 200 또는 없으면 정상으로 판단
def _is_api_ok(parsed: dict) -> bool:
    try:
        meta = parsed.get("MetaData") or {}
        code = (meta.get("resultSummary") or {}).get("statusCode")
        return code is None or str(code) == "200"
    except Exception:
        return True

# API 단일 페이지 호출, 실패 시 최대 3회 exponential backoff 재시도
async def _fetch_page(
    client: ScienceOnClient,
    keyword: str,
    page: int,
    start_year: int,
    end_year: int,
) -> str | None:
    for attempt in range(MAX_RETRIES):
        try:
            return await client.search_articles(
                query=keyword,
                page=page,
                size=ROW_COUNT,
                search_field="KW",
                start_year=start_year,
                end_year=end_year,
            )
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                print(f"  [오류] '{keyword}' p{page} 최종 실패: {exc}")
                return None
            wait = 2 ** attempt
            print(f"  [재시도] '{keyword}' p{page} 실패 → {wait}초 대기 ({attempt + 1}/{MAX_RETRIES - 1})")
            await asyncio.sleep(wait)
    return None

# 키워드 1개로 페이지네이션하며 CN 기준 중복 제거해 논문 수집
async def _collect_keyword(
    client: ScienceOnClient,
    keyword: str,
    seen_cns: set[str],
    years: int,
) -> list[dict]:
    end_year = CURRENT_YEAR - 1
    start_year = end_year - years
    new_papers: list[dict] = []
    page = 1

    while len(seen_cns) < MAX_TOTAL:
        xml = await _fetch_page(client, keyword, page, start_year, end_year)
        if xml is None:
            break

        parsed = parse_scienceon_xml(xml)
        if not _is_api_ok(parsed):
            print(f"  [경고] '{keyword}' p{page} API 오류 응답 → 중단")
            break

        records = _find_records(parsed)
        if not records:
            break

        added = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            flat = _flatten_record(record)
            cn = flat.get("CN")
            if not cn or cn in seen_cns:
                continue
            seen_cns.add(cn)
            new_papers.append({field: flat.get(field) for field in SAVE_FIELDS})
            added += 1
            if len(seen_cns) >= MAX_TOTAL:
                break

        print(
            f"  [{keyword}] {years}년치 p{page}: "
            f"+{added}건 (응답 {len(records)}건) → 누적 {len(seen_cns)}건 / {MAX_TOTAL}건"
        )

        if len(records) < ROW_COUNT or len(seen_cns) >= MAX_TOTAL:
            break

        page += 1

    return new_papers


async def main() -> None:
    from app.core.settings import settings

    if not settings.scienceon_client_id or not settings.scienceon_token:
        print("[오류] SCIENCEON_CLIENT_ID 또는 SCIENCEON_TOKEN이 .env에 설정되지 않았습니다.")
        sys.exit(1)

    client = ScienceOnClient()
    seen_cns: set[str] = set()
    all_papers: list[dict] = []
    year_range_used = 1

    year_range_used = 2

    print(f"=== ScienceON 생명공학 논문 수집 시작 (목표: {MAX_TOTAL}건) ===")
    print(f"    기간: {CURRENT_YEAR - 3}~{CURRENT_YEAR - 1} (2년치)\n")

    # ── 2년치 직접 수집 ─────────────────────────────────────
    for keyword in KEYWORDS:
        if len(seen_cns) >= MAX_TOTAL:
            break
        print(f"\n▶ [{keyword}] 2년치 수집 중 ({CURRENT_YEAR - 3}~{CURRENT_YEAR - 1})...")
        papers = await _collect_keyword(client, keyword, seen_cns, years=2)
        all_papers.extend(papers)

    print(f"\n── 2년치 수집 완료: {len(seen_cns)}건 ──")

    # ── 저장 ────────────────────────────────────────────────
    output = {
        "meta": {
            "collected_at": datetime.now().isoformat(),
            "total_count": len(all_papers),
            "year_range": f"{CURRENT_YEAR - 1 - year_range_used}~{CURRENT_YEAR - 1}",
            "keywords": KEYWORDS,
        },
        "papers": all_papers,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n=== 수집 완료: {len(all_papers)}건 → {OUTPUT_PATH} ===")

# 8개 키워드 순회하며 1,200건 채워지면 중단, raw JSON으로 저장
if __name__ == "__main__":
    asyncio.run(main())
