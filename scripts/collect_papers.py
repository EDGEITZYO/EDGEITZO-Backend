"""ScienceON 생명공학 논문 수집 스크립트"""
from __future__ import annotations

import argparse
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

KEYWORDS = ["생명공학", "바이오", "유전자", "단백질", "세포공학", "줄기세포", "효소", "미생물", "유전체", "면역", "항체"]

SAVE_FIELDS = [
    "CN", "DBCode", "Title", "Title2", "Abstract", "Abstract2",
    "Keyword", "Keyword2", "ISSN", "DOI", "Pubyear", "Pubdate", "JournalName", "Author",
    "FulltextFlag",  # 선별 가점용 (원문 공개 여부)
]

MAX_TOTAL = 2000
ROW_COUNT = 100
MAX_RETRIES = 3
CURRENT_YEAR = datetime.now().year
DEFAULT_END_YEAR   = CURRENT_YEAR - 1   # 2025
DEFAULT_START_YEAR = DEFAULT_END_YEAR - 9  # 2016 (10년치)
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
    search_field: str = "KW",
) -> str | None:
    for attempt in range(MAX_RETRIES):
        try:
            return await client.search_articles(
                query=keyword,
                page=page,
                size=ROW_COUNT,
                search_field=search_field,
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
# exclude_dbcodes: 해당 DBCode는 중복 체크에는 포함하되 저장·카운트에서 제외
async def _collect_keyword(
    client: ScienceOnClient,
    keyword: str,
    seen_cns: set[str],
    valid_count: list[int],
    start_year: int,
    end_year: int,
    max_total: int,
    exclude_dbcodes: frozenset[str] = frozenset(),
    only_dbcodes: frozenset[str] = frozenset(),
    search_field: str = "KW",
) -> list[dict]:
    new_papers: list[dict] = []
    page = 1

    while valid_count[0] < max_total:
        xml = await _fetch_page(client, keyword, page, start_year, end_year, search_field)
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
            dbcode = flat.get("DBCode", "")
            if dbcode in exclude_dbcodes:
                continue
            if only_dbcodes and dbcode not in only_dbcodes:
                continue
            new_papers.append({field: flat.get(field) for field in SAVE_FIELDS})
            valid_count[0] += 1
            added += 1
            if valid_count[0] >= max_total:
                break

        print(
            f"  [{keyword}] p{page}: "
            f"+{added}건 (응답 {len(records)}건) → 유효 누적 {valid_count[0]}건 / {max_total}건"
        )

        if len(records) < ROW_COUNT or valid_count[0] >= max_total:
            break

        page += 1

    return new_papers


async def main(
    max_total: int = MAX_TOTAL,
    keywords: list[str] | None = None,
    output_path: Path = OUTPUT_PATH,
    start_year: int = DEFAULT_START_YEAR,
    end_year: int = DEFAULT_END_YEAR,
    exclude_dbcodes: frozenset[str] = frozenset(),
    only_dbcodes: frozenset[str] = frozenset(),
    merge: bool = False,
    search_field: str = "KW",
) -> None:
    from app.core.settings import settings

    if not settings.scienceon_client_id or not settings.scienceon_token:
        print("[오류] SCIENCEON_CLIENT_ID 또는 SCIENCEON_TOKEN이 .env에 설정되지 않았습니다.")
        sys.exit(1)

    kws = keywords if keywords else KEYWORDS

    client = ScienceOnClient()
    seen_cns: set[str] = set()
    valid_count: list[int] = [0]
    all_papers: list[dict] = []

    # --merge: 기존 파일 로드 후 seen_cns 선점 (덮어쓰지 않고 추가)
    if merge and output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        all_papers = existing.get("papers", [])
        seen_cns = {p["CN"] for p in all_papers if p.get("CN")}
        valid_count[0] = len(all_papers)
        print(f"[머지] 기존 {len(all_papers)}건 로드 (CN {len(seen_cns)}개 선점)")

    # only_dbcodes: 해당 DBCode만 수집 (exclude_dbcodes와 병용 가능)
    effective_exclude = exclude_dbcodes
    if only_dbcodes:
        print(f"    수집 DBCode: {', '.join(sorted(only_dbcodes))}")

    print(f"=== ScienceON 생명공학 논문 수집 시작 (목표: {max_total}건) ===")
    print(f"    기간: {start_year}~{end_year} ({end_year - start_year + 1}년치)")
    print(f"    검색 필드: {search_field}")
    if effective_exclude:
        print(f"    제외 DBCode: {', '.join(sorted(effective_exclude))}")
    print(f"    키워드 {len(kws)}개: {', '.join(kws)}\n")

    for keyword in kws:
        if valid_count[0] >= max_total:
            break
        print(f"\n▶ [{keyword}] 수집 중 ({start_year}~{end_year})...")
        papers = await _collect_keyword(
            client, keyword, seen_cns, valid_count,
            start_year=start_year, end_year=end_year,
            max_total=max_total,
            exclude_dbcodes=effective_exclude,
            only_dbcodes=only_dbcodes,
            search_field=search_field,
        )
        all_papers.extend(papers)

    added = valid_count[0] - (len(all_papers) - len(papers) if merge else 0)
    print(f"\n── 수집 완료: 총 {valid_count[0]}건 ──")

    output = {
        "meta": {
            "collected_at": datetime.now().isoformat(),
            "total_count": len(all_papers),
            "year_range": f"{start_year}~{end_year}",
            "keywords": kws,
            "max_total": max_total,
            "excluded_dbcodes": sorted(effective_exclude),
            "only_dbcodes": sorted(only_dbcodes),
        },
        "papers": all_papers,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n=== 수집 완료: {len(all_papers)}건 → {output_path} ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ScienceON 논문 수집")
    parser.add_argument(
        "--max-total", type=int, default=MAX_TOTAL,
        help=f"수집 목표 건수 (기본: {MAX_TOTAL})",
    )
    parser.add_argument(
        "--keywords", type=str, default=None,
        help="쉼표 구분 키워드 (예: '생명공학,바이오,유전자'). 미지정 시 기본 8개 사용",
    )
    parser.add_argument(
        "--keywords-file", type=Path, default=None,
        help="키워드 파일 경로 (한 줄에 키워드 하나). --keywords보다 우선",
    )
    parser.add_argument(
        "--start-year", type=int, default=DEFAULT_START_YEAR,
        help=f"수집 시작 연도 (기본: {DEFAULT_START_YEAR})",
    )
    parser.add_argument(
        "--end-year", type=int, default=DEFAULT_END_YEAR,
        help=f"수집 종료 연도 (기본: {DEFAULT_END_YEAR})",
    )
    parser.add_argument(
        "--exclude-dbcode", type=str, default="",
        help="저장에서 제외할 DBCode (쉼표 구분, 예: DIKO,CFKO)",
    )
    parser.add_argument(
        "--only-dbcode", type=str, default="",
        help="이 DBCode만 저장 (쉼표 구분, 예: DIKO,CFKO). 미지정 시 전체 저장",
    )
    parser.add_argument(
        "--merge", action="store_true",
        help="기존 출력 파일에 추가 (덮어쓰지 않음). CN 기준 중복 자동 제거",
    )
    parser.add_argument(
        "--search-field", type=str, default="KW",
        help="검색 필드 (KW: 키워드, TI: 제목, 기본: KW)",
    )
    parser.add_argument(
        "--out", type=Path, default=OUTPUT_PATH,
        help=f"출력 JSON 경로 (기본: {OUTPUT_PATH})",
    )
    args = parser.parse_args()

    if args.keywords_file:
        kws = [
            line.strip()
            for line in args.keywords_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    elif args.keywords:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        kws = None  # main()이 KEYWORDS 기본값 사용

    exclude = frozenset(c.strip().upper() for c in args.exclude_dbcode.split(",") if c.strip())
    only   = frozenset(c.strip().upper() for c in args.only_dbcode.split(",")   if c.strip())
    asyncio.run(main(
        max_total=args.max_total,
        keywords=kws,
        output_path=args.out,
        start_year=args.start_year,
        end_year=args.end_year,
        exclude_dbcodes=exclude,
        only_dbcodes=only,
        merge=args.merge,
        search_field=args.search_field,
    ))
