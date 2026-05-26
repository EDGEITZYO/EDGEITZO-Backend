"""Phase 1 백필 스크립트.

Step 1: journals.kci_field  — KCI CSV '학술지 연구분야' 컬럼으로 ISSN 기반 백필
Step 2: papers.journal_id   — papers.issn → journals(p_issn, e_issn) 매칭으로 백필

미매칭: data/unmatched/journal_field_backfill.json

사용법:
  python scripts/backfill_journal_fields.py
  python scripts/backfill_journal_fields.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

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

from sqlalchemy import select, text
from app.core.database import AsyncSessionLocal
from app.models.journal import Journal
from app.models.paper import Paper

UNMATCHED_PATH = PROJECT_ROOT / "data" / "unmatched" / "journal_field_backfill.json"


def _normalize_issn(raw: Any) -> str | None:
    if not raw or (isinstance(raw, float)):
        return None
    s = str(raw).strip().replace("-", "").replace(" ", "").upper()
    if not s or s in ("NAN", ""):
        return None
    if re.fullmatch(r"[\dX]{8}", s):
        return f"{s[:4]}-{s[4:]}"
    if re.fullmatch(r"[\dX]{4}-[\dX]{4}", s):
        return s
    return None


def _issn_to_hyphen(raw: str | None) -> str | None:
    """8자리 숫자 → XXXX-XXXX (papers.issn 포맷 변환)."""
    if not raw:
        return None
    s = str(raw).strip().replace("-", "").upper()
    if re.fullmatch(r"[\dX]{8}", s):
        return f"{s[:4]}-{s[4:]}"
    return raw if re.fullmatch(r"[\dX]{4}-[\dX]{4}", raw) else None


def _load_kci_csv() -> pd.DataFrame:
    raw_dir = PROJECT_ROOT / "data" / "kci_raw"
    candidates = list(raw_dir.glob("*KCI*.csv")) + list(raw_dir.glob("*kci*.csv"))
    if not candidates:
        raise FileNotFoundError(f"KCI CSV 없음: {raw_dir}")
    csv_path = max(candidates, key=lambda p: (re.search(r"(\d{8})", p.name) or type("", (), {"group": lambda *a: "0"})()).group(1))
    for enc in ("euc-kr", "cp949", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(csv_path, encoding=enc, dtype=str)
            df.columns = [c.strip() for c in df.columns]
            df["_p_issn"] = df["국제표준연속 간행물번호(종이식)"].apply(_normalize_issn)
            df["_e_issn"] = df["국제표준연속 간행물번호(전자식)"].apply(_normalize_issn)
            print(f"  KCI CSV: {csv_path.name} ({len(df):,}행, enc={enc})")
            return df
        except (UnicodeDecodeError, KeyError):
            continue
    raise ValueError(f"KCI CSV 인코딩/컬럼 오류: {csv_path}")


# ---------------------------------------------------------------------------
# Step 1: journals.kci_field 백필
# ---------------------------------------------------------------------------

async def step1_backfill_kci_field(df: pd.DataFrame, *, dry_run: bool) -> dict[str, int]:
    """KCI CSV → journals.kci_field."""
    # ISSN → field 룩업 테이블 구성
    issn_to_field: dict[str, str] = {}
    for _, row in df.iterrows():
        field = str(row.get("학술지 연구분야") or "").strip()
        if not field or field.lower() == "nan":
            continue
        for issn in [row["_p_issn"], row["_e_issn"]]:
            if issn:
                issn_to_field[issn] = field

    updated = skipped = no_field = 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Journal))
        journals = result.scalars().all()

        for j in journals:
            field = None
            for issn in [j.p_issn, j.e_issn] + (j.issn or []):
                issn_norm = _normalize_issn(issn) if issn else None
                if issn_norm and issn_norm in issn_to_field:
                    field = issn_to_field[issn_norm]
                    break

            if not field:
                no_field += 1
                continue

            if j.kci_field == field:
                skipped += 1
                continue

            if not dry_run:
                j.kci_field = field
            updated += 1

        if not dry_run:
            await session.commit()

    return {"updated": updated, "skipped_same": skipped, "no_field": no_field}


# ---------------------------------------------------------------------------
# Step 2: papers.journal_id 백필
# ---------------------------------------------------------------------------

async def step2_backfill_journal_id(*, dry_run: bool) -> tuple[dict[str, int], list[dict]]:
    """papers.issn → journals(p_issn, e_issn) 매칭으로 journal_id 백필."""
    updated = already_set = unmatched_cnt = 0
    unmatched: list[dict] = []

    async with AsyncSessionLocal() as session:
        # journals ISSN → id 룩업
        jresult = await session.execute(select(Journal))
        issn_to_journal_id: dict[str, int] = {}
        for j in jresult.scalars().all():
            for issn in ([j.p_issn, j.e_issn] + (j.issn or [])):
                if issn:
                    norm = _normalize_issn(issn)
                    if norm:
                        issn_to_journal_id[norm] = j.id

        # JAKO + JAFO 논문 중 journal_id NULL 인 것만
        presult = await session.execute(
            select(Paper).where(
                Paper.db_code.in_(["JAKO", "JAFO"]),
                Paper.journal_id.is_(None),
            )
        )
        papers = presult.scalars().all()

        for p in papers:
            issn_norm = _issn_to_hyphen(p.issn)
            jid = issn_to_journal_id.get(issn_norm) if issn_norm else None

            if jid:
                if not dry_run:
                    p.journal_id = jid
                updated += 1
            else:
                unmatched.append({
                    "scienceon_cn": p.scienceon_cn,
                    "db_code": p.db_code,
                    "issn": p.issn,
                    "issn_norm": issn_norm,
                    "title": str(p.title)[:60],
                })
                unmatched_cnt += 1

        # 이미 journal_id 있는 것
        presult2 = await session.execute(
            select(Paper).where(
                Paper.db_code.in_(["JAKO", "JAFO"]),
                Paper.journal_id.isnot(None),
            )
        )
        already_set = len(presult2.scalars().all())

        if not dry_run:
            await session.commit()

    return {"updated": updated, "already_set": already_set, "unmatched": unmatched_cnt}, unmatched


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

async def run(*, dry_run: bool) -> None:
    print("=== Step 1: journals.kci_field 백필 ===")
    df = _load_kci_csv()
    stats1 = await step1_backfill_kci_field(df, dry_run=dry_run)
    print(f"  kci_field 업데이트: {stats1['updated']}건")
    print(f"  이미 동일값:        {stats1['skipped_same']}건")
    print(f"  ISSN 매칭 실패:     {stats1['no_field']}건")

    print("\n=== Step 2: papers.journal_id 백필 ===")
    stats2, unmatched = await step2_backfill_journal_id(dry_run=dry_run)
    print(f"  journal_id 연결:    {stats2['updated']}건")
    print(f"  이미 설정됨:        {stats2['already_set']}건")
    print(f"  매칭 실패:          {stats2['unmatched']}건")

    if unmatched:
        UNMATCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNMATCHED_PATH.write_text(
            json.dumps(unmatched, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  미매칭 목록 저장: {UNMATCHED_PATH}")
        # 미매칭 저널명 그룹 출력
        from collections import Counter
        issn_cnt = Counter(u["issn"] for u in unmatched)
        print("  미매칭 ISSN 상위:")
        for issn, cnt in issn_cnt.most_common(10):
            sample = next(u for u in unmatched if u["issn"] == issn)
            print(f"    {issn or 'NULL':12s} {cnt}건 | {sample['title'][:40]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="kci_field + journal_id 백필")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print("[dry-run] DB 쓰기 생략\n")
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
