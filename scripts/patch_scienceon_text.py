"""data/parsed/scienceon_enriched.json + papers DB의 이중 이스케이프/서식 태그 정리.

ScienceON 원본 XML의 Abstract/Title 계열 필드에 이중 이스케이프된 엔티티
(&amp;amp;, &amp;#xD; 등)와 서식 태그(<TEX>, <SUP>, <SUB>, <B>, <I>, <P>)가
그대로 남아있는 문제를 의미에 맞게 변환한다.

  &amp;/&lt;/&gt;/&#xD; 등  → html.unescape (실제 문자/개행으로 복원)
  <TEX>$..$</TEX>           → 태그만 제거, LaTeX($..$)는 보존
  <SUP>n</SUP>/<SUB>n</SUB> → 유니코드 위/아래첨자 (매핑 안 되는 문자는 원문 보존)
  <B>..</B> / <I>..</I>     → 마크다운 **굵게** / *기울임*
  <P>..</P>                 → 문단 구분(개행 두 번)

태그 alternation에 알려진 태그명만 하드코딩되어 있어 `<Parasite>`, `<0.001` 같은
꺾쇠괄호/부등호 오탐은 애초에 매칭되지 않는다.

사용법 (로컬, data/parsed/scienceon_enriched.json이 있는 환경):
  python scripts/patch_scienceon_text.py --dry-run
  python scripts/patch_scienceon_text.py            # JSON 파일 정리 (원본은 .bak로 백업)
  python scripts/patch_scienceon_text.py --db        # DB papers 테이블까지 UPDATE

사용법 (프로덕션 등 위 JSON 파일이 없는 환경 — DB를 직접 읽어서 정제):
  python scripts/patch_scienceon_text.py --dry-run --db
  python scripts/patch_scienceon_text.py --db
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
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

from sqlalchemy import text as sql_text

from app.core.database import AsyncSessionLocal

JSON_PATH = PROJECT_ROOT / "data/parsed/scienceon_enriched.json"
TEXT_FIELDS = ["Abstract", "Abstract2", "Title", "Title2"]
# JSON 필드명 → DB 컬럼명
DB_COLUMNS = {"Abstract": "abstract", "Abstract2": "abstract_en", "Title": "title", "Title2": "title_en"}

_SUP_MAP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
}
_SUB_MAP = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "o": "ₒ", "x": "ₓ",
}

# 알려진 6개 태그만 하드코딩 — <Parasite>, <0.001 같은 무관한 꺾쇠괄호는 애초에 매칭 안 됨
_TAG_PAT = re.compile(r"<(TEX|SUP|SUB|B|I|P)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
# 원본 자체가 닫는 태그 없이 잘린 경우(주로 마지막 문단의 <P>) 대비 안전장치.
# 짝을 못 찾은 마커만 제거 — 화이트리스트 태그명이라 <Parasite> 같은 오탐은 여기도 안 걸림.
_STRAY_TAG_PAT = re.compile(r"</?(TEX|SUP|SUB|B|I|P)\b[^>]*>", re.IGNORECASE)


def _to_script(content: str, mapping: dict[str, str]) -> str:
    if content and all(ch in mapping for ch in content):
        return "".join(mapping[ch] for ch in content)
    return content  # 매핑 안 되는 문자가 섞이면 위/아래첨자 변환 없이 원문 보존


def _replace_tag(match: re.Match) -> str:
    tag = match.group(1).upper()
    content = _TAG_PAT.sub(_replace_tag, match.group(2))  # 중첩 태그 먼저 처리
    if tag == "TEX":
        return content
    if tag == "SUP":
        return _to_script(content, _SUP_MAP)
    if tag == "SUB":
        return _to_script(content, _SUB_MAP)
    if tag == "B":
        return f"**{content}**"
    if tag == "I":
        return f"*{content}*"
    if tag == "P":
        return f"{content}\n\n"
    return content


def clean_text(value: str | None) -> str | None:
    if not value:
        return value

    text = str(value)
    # 이중 이스케이프(&amp;#xD; 등) 대응 위해 두 번 unescape.
    # 단일 이스케이프 값에는 두 번째 호출이 no-op이라 안전함.
    text = html.unescape(text)
    text = html.unescape(text)

    text = _TAG_PAT.sub(_replace_tag, text)
    text = _STRAY_TAG_PAT.sub(lambda m: "\n\n" if m.group(1).upper() == "P" else "", text)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text or None


async def _update_db(changed: list[tuple[str, str, str]], *, dry_run: bool) -> int:
    """changed: (scienceon_cn, db_column, new_value) 목록."""
    if dry_run or not changed:
        return 0
    updated = 0
    async with AsyncSessionLocal() as session:
        for cn, column, value in changed:
            result = await session.execute(
                sql_text(f"UPDATE papers SET {column} = :val WHERE scienceon_cn = :cn"),
                {"val": value, "cn": cn},
            )
            updated += result.rowcount
        await session.commit()
    return updated


async def run_db_native(*, dry_run: bool) -> None:
    """data/parsed/scienceon_enriched.json이 없는 환경(프로덕션 등)에서
    papers 테이블 값을 직접 읽어 정제 후 그대로 다시 UPDATE."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sql_text(
                "SELECT scienceon_cn, abstract, abstract_en, title, title_en "
                "FROM papers WHERE scienceon_cn IS NOT NULL"
            )
        )
        rows = result.fetchall()
        print(f"전체 대상(scienceon_cn 보유): {len(rows)}건")

        updates: list[tuple[str, str, str]] = []
        examples: list[tuple[str, str, str, str]] = []
        for cn, abstract, abstract_en, title, title_en in rows:
            for column, value in [
                ("abstract", abstract), ("abstract_en", abstract_en),
                ("title", title), ("title_en", title_en),
            ]:
                if not value:
                    continue
                cleaned = clean_text(value)
                if cleaned != value:
                    updates.append((cn, column, cleaned))
                    if len(examples) < 3:
                        examples.append((column, cn, value[:200], cleaned[:200]))

        print(f"정리 대상 필드값: {len(updates)}건")

        if dry_run:
            print("\n[dry-run] DB 쓰기 생략. 변경 예시 3개:")
            for column, cn, before, after in examples:
                print(f"\n--- {column} (CN={cn}) ---")
                print("BEFORE:", before)
                print("AFTER :", after)
            return

        updated = 0
        for cn, column, val in updates:
            r = await session.execute(
                sql_text(f"UPDATE papers SET {column} = :val WHERE scienceon_cn = :cn"),
                {"val": val, "cn": cn},
            )
            updated += r.rowcount
        await session.commit()
        print(f"DB 업데이트: {updated}건")


async def run(*, dry_run: bool, update_db: bool) -> None:
    if not JSON_PATH.exists():
        await run_db_native(dry_run=dry_run)
        return

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    papers: list[dict] = data["papers"]
    print(f"전체 레코드: {len(papers)}건")

    # 이전 실행으로 JSON이 이미 정리된 상태라면(.bak 존재), 정제 전 원본은
    # 지금의 live JSON이 아니라 .bak에 있다. 그래야 --db만 다시 돌려도
    # 어떤 필드가 원래 오염됐었는지 판단할 수 있다.
    backup_path = JSON_PATH.with_suffix(".json.bak")
    ref_by_cn: dict[str, dict] | None = None
    if backup_path.exists():
        ref_data = json.loads(backup_path.read_text(encoding="utf-8"))
        ref_by_cn = {p.get("CN"): p for p in ref_data["papers"]}

    changed_json = 0
    db_changes: list[tuple[str, str, str]] = []
    examples: list[tuple[str, str, str, str]] = []

    for p in papers:
        cn = (p.get("CN") or "").strip()
        ref = ref_by_cn.get(cn, {}) if ref_by_cn is not None else p
        for field in TEXT_FIELDS:
            original = ref.get(field)
            if not original:
                continue
            cleaned = clean_text(original)
            if cleaned != original:
                changed_json += 1
                if len(examples) < 3:
                    examples.append((field, cn, str(original)[:200], str(cleaned)[:200]))
                if not dry_run:
                    p[field] = cleaned  # ref(.bak 또는 live)로부터 재계산한 값을 live JSON에 반영
                if update_db and cn:
                    db_changes.append((cn, DB_COLUMNS[field], cleaned))

    print(f"정리 대상 필드값: {changed_json}건")

    if dry_run:
        print("\n[dry-run] 파일/DB 쓰기 생략. 변경 예시 3개:")
        for field, cn, before, after in examples:
            print(f"\n--- {field} (CN={cn}) ---")
            print("BEFORE:", before)
            print("AFTER :", after)
        return

    if not backup_path.exists():
        shutil.copy2(JSON_PATH, backup_path)
        print(f"백업 생성: {backup_path}")
    else:
        print(f"기존 백업 유지: {backup_path} (원본은 이걸 기준으로 재계산)")

    JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 저장 완료: {JSON_PATH}")

    if update_db:
        db_updated = await _update_db(db_changes, dry_run=dry_run)
        print(f"DB 업데이트: {db_updated}건")
    else:
        print("DB는 건드리지 않음 (--db 플래그로 실행하면 papers 테이블도 업데이트)")


def main() -> None:
    parser = argparse.ArgumentParser(description="ScienceON 텍스트 이스케이프/태그 정리")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", action="store_true", help="papers 테이블까지 UPDATE")
    args = parser.parse_args()
    if args.dry_run:
        print("[dry-run] 파일/DB 쓰기 생략\n")
    asyncio.run(run(dry_run=args.dry_run, update_db=args.db))


if __name__ == "__main__":
    main()
