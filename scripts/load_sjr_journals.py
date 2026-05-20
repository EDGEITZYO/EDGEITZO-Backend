"""
SJR Journals 적재 스크립트.

사용법:
  python scripts/load_sjr_journals.py
  python scripts/load_sjr_journals.py --dry-run   # 적재 없이 통계만
  python scripts/load_sjr_journals.py --reset     # 기존 journals 삭제 후 재적재
  python scripts/load_sjr_journals.py --dir data/sjr_raw  # 폴더 지정
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# .env 수동 로드 (스크립트 직접 실행 시 pydantic-settings보다 먼저 필요)
_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k:
            os.environ.setdefault(_k, _v)

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.models.journal import Journal  # noqa: E402

# DB insert 시 제외할 컬럼 (DataFrame 연산용이지 모델 컬럼 아님)
_DF_ONLY_COLS = {"sjr_year", "issn_raw", "categories_raw"}

# upsert 시 갱신할 컬럼 (id, sjr_sourceid 제외)
_UPSERT_COLS = [
    "title", "type", "issn", "publisher",
    "sjr_score", "sjr_best_quartile", "h_index",
    "sci_indexed", "kci_indexed", "if_value",
    "categories", "country", "coverage", "updated_at",
]


# ---------------------------------------------------------------------------
# CSV 파싱
# ---------------------------------------------------------------------------

def extract_year(filename: str) -> int | None:
    match = re.search(r"(\d{4})", filename)
    if match:
        year = int(match.group(1))
        if 2000 <= year <= 2030:
            return year
    return None


def parse_issn(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    s = str(value).strip()
    if not s:
        return []
    parts = re.split(r"[,\s]+", s)
    return [p.strip() for p in parts if p.strip()]


def parse_categories(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    s = str(value).strip()
    if not s:
        return []
    return [p.strip() for p in s.split(";") if p.strip()]


def read_sjr_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        sep=";",
        dtype=str,
        encoding="utf-8",
    )
    # 중복 컬럼명 처리 (예: Publisher가 두 번 나오는 경우 첫 번째만 유지)
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def clean_dataframe(df: pd.DataFrame, year: int) -> pd.DataFrame:
    df = df.rename(columns=lambda c: c.strip())

    col_map = {
        "Sourceid": "sjr_sourceid",
        "Title": "title",
        "Type": "type",
        "Issn": "issn_raw",
        "Publisher": "publisher",
        "SJR": "sjr_score",
        "SJR Best Quartile": "sjr_best_quartile",
        "H index": "h_index",
        "Country": "country",
        "Coverage": "coverage",
        "Categories": "categories_raw",
    }
    missing = [c for c in col_map if c not in df.columns]
    if missing:
        raise ValueError(f"CSV missing columns: {missing} in {df.columns.tolist()}")

    df = df[list(col_map.keys())].rename(columns=col_map).copy()

    df["sjr_year"] = year
    # 유럽식 소수점 처리: "11,748" → 11.748
    df["sjr_score"] = (
        df["sjr_score"].str.replace(",", ".", regex=False).pipe(pd.to_numeric, errors="coerce")
    )
    df["h_index"] = pd.to_numeric(df["h_index"], errors="coerce").astype("Int64")
    df["title"] = df["title"].str.strip()
    df["sjr_sourceid"] = df["sjr_sourceid"].str.strip()

    # varchar 한도 초과 방지 (truncate)
    _limits = {"sjr_sourceid": 50, "title": 500, "type": 50, "publisher": 300,
               "sjr_best_quartile": 2, "country": 100, "coverage": 100}
    for _col, _limit in _limits.items():
        if _col in df.columns:
            df[_col] = df[_col].str[:_limit]

    df["issn"] = df["issn_raw"].apply(parse_issn)
    df["categories"] = df["categories_raw"].apply(parse_categories)

    df["sci_indexed"] = df["sjr_best_quartile"].isin(["Q1", "Q2"])
    df["kci_indexed"] = False
    df["if_value"] = None

    final_cols = [
        "sjr_sourceid", "title", "type", "issn", "publisher",
        "sjr_score", "sjr_best_quartile", "h_index",
        "country", "coverage", "categories",
        "sjr_year", "sci_indexed", "kci_indexed", "if_value",
    ]
    return df[final_cols].reset_index(drop=True)


def load_all_files(data_dir: Path) -> pd.DataFrame:
    csv_files = sorted(data_dir.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in {data_dir}")

    all_dfs: list[pd.DataFrame] = []
    for path in csv_files:
        year = extract_year(path.name)
        if year is None:
            print(f"[skip] {path.name}: cannot extract year")
            continue
        print(f"[read] {path.name} (year={year})", end="", flush=True)
        df = read_sjr_csv(path)
        df_clean = clean_dataframe(df, year)
        all_dfs.append(df_clean)
        print(f" → {len(df_clean):,} rows")

    combined = pd.concat(all_dfs, ignore_index=True)
    print(f"\n[combined] {len(combined):,} rows total (before dedup)")
    return combined


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    before = len(df)
    df_sorted = df.sort_values("sjr_year", ascending=False)
    df_dedup = df_sorted.drop_duplicates(subset="sjr_sourceid", keep="first")
    after = len(df_dedup)
    print(f"[dedup]   {before:,} → {after:,} rows (removed {before - after:,} dupes)")
    return df_dedup.reset_index(drop=True)


# ---------------------------------------------------------------------------
# DB 적재
# ---------------------------------------------------------------------------

def _to_db_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → list[dict], pandas NaN/NA → None, sjr_year 등 제거."""
    records = []
    drop_cols = _DF_ONLY_COLS & set(df.columns)
    df_db = df.drop(columns=list(drop_cols))

    for row in df_db.itertuples(index=False):
        d: dict[str, Any] = {}
        for col in df_db.columns:
            val = getattr(row, col)
            # pandas NA / float NaN → None
            if val is None:
                d[col] = None
            elif isinstance(val, float) and math.isnan(val):
                d[col] = None
            elif hasattr(val, "__class__") and val.__class__.__name__ in ("NAType", "NaTType"):
                d[col] = None
            else:
                # pandas Int64 → Python int
                try:
                    import pandas as _pd
                    if isinstance(val, _pd.NA.__class__):
                        d[col] = None
                        continue
                except Exception:
                    pass
                d[col] = val
        records.append(d)
    return records


async def _reset_journals() -> int:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("DELETE FROM journals"))
        await session.commit()
        return result.rowcount


async def _upsert_journals(records: list[dict[str, Any]], chunk_size: int = 500) -> int:
    inserted = 0
    async with AsyncSessionLocal() as session:
        for i in range(0, len(records), chunk_size):
            chunk = records[i : i + chunk_size]
            stmt = pg_insert(Journal).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["sjr_sourceid"],
                set_={col: stmt.excluded[col] for col in _UPSERT_COLS},
            )
            await session.execute(stmt)
            await session.commit()
            inserted += len(chunk)
            print(f"  upserted {inserted:,}/{len(records):,}")
    return inserted


async def _verify() -> None:
    async with AsyncSessionLocal() as session:
        total = (await session.execute(select(func.count()).select_from(Journal))).scalar()
        print(f"\n[verify] Total journals in DB: {total:,}")

        samples = (await session.execute(select(Journal).limit(5))).scalars().all()
        print("\nSample 5 rows:")
        for j in samples:
            title_preview = (j.title or "")[:50]
            print(
                f"  {j.sjr_sourceid} | {title_preview} | "
                f"SJR={j.sjr_score} {j.sjr_best_quartile} | "
                f"ISSN={j.issn} | SCI={j.sci_indexed}"
            )

        q_rows = (
            await session.execute(
                select(Journal.sjr_best_quartile, func.count())
                .group_by(Journal.sjr_best_quartile)
                .order_by(Journal.sjr_best_quartile)
            )
        ).all()
        print("\nQuartile distribution:")
        for q, c in q_rows:
            print(f"  {q or 'NULL':>4}: {c:,}")

        dup_count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT sjr_sourceid FROM journals"
                    "  GROUP BY sjr_sourceid HAVING COUNT(*) > 1"
                    ") t"
                )
            )
        ).scalar()
        status = "OK (0 duplicates)" if dup_count == 0 else f"WARN: {dup_count} duplicate sourceids!"
        print(f"\nDuplicate check: {status}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Load SJR CSV data into journals table.")
    parser.add_argument("--dir", default="data/sjr_raw", help="CSV folder path")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no DB write")
    parser.add_argument("--reset", action="store_true", help="Delete all journals before insert")
    parser.add_argument("--chunk-size", type=int, default=500, help="Insert batch size")
    args = parser.parse_args()

    data_dir = (PROJECT_ROOT / args.dir).resolve()
    if not data_dir.exists():
        print(f"[error] Directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    df_combined = load_all_files(data_dir)
    df_final = deduplicate(df_combined)

    if args.dry_run:
        print("\n[dry-run] skipping DB write")
        print(df_final[["sjr_sourceid", "title", "sjr_score", "sjr_best_quartile", "issn"]].head(10).to_string())
        return

    records = _to_db_records(df_final)

    async def _run() -> None:
        if args.reset:
            deleted = await _reset_journals()
            print(f"[reset]   Deleted {deleted:,} existing journals")
        print(f"\n[insert]  {len(records):,} records → journals table")
        inserted = await _upsert_journals(records, chunk_size=args.chunk_size)
        print(f"[done]    Upserted {inserted:,} journals")
        await _verify()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
