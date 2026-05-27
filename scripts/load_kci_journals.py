"""KCI 등재지 마스터 CSV → Postgres journals 테이블 적재.

사용법:
  python scripts/load_kci_journals.py
  python scripts/load_kci_journals.py --dir data/kci_raw
  python scripts/load_kci_journals.py --dry-run

CSV 파일: data/kci_raw/한국연구재단_KCI학술지정보_YYYYMMDD.csv (EUC-KR)
  - 파일명 날짜(YYYYMMDD) 기준 최신 파일 자동 탐색
  - 수동 다운로드: https://www.kci.go.kr/kciportal/po/openapi/openApiInfo.kci
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import os
_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.models.journal import Journal

KCI_STATUS_MAP = {
    "우수등재": "excellent",
    "등재": "accredited",
    "등재후보": "candidate",
}

# ISSN 정규화: 8자리 숫자(+X) → XXXX-XXXX
def _normalize_issn(raw: Any) -> str | None:
    if not raw or (isinstance(raw, float)):
        return None
    s = str(raw).strip().replace("-", "").replace(" ", "").upper()
    if not s or s in ("NAN", ""):
        return None
    # 이미 8자리(숫자+X)인 경우만 처리
    if re.fullmatch(r"[\dX]{8}", s):
        return f"{s[:4]}-{s[4:]}"
    # 이미 XXXX-XXXX 형태
    if re.fullmatch(r"[\dX]{4}-[\dX]{4}", s.upper()):
        return s.upper()
    return None


def _find_latest_csv(data_dir: Path) -> Path | None:
    candidates = list(data_dir.glob("*KCI*.csv")) + list(data_dir.glob("*kci*.csv"))
    if not candidates:
        return None
    # 파일명에서 날짜(YYYYMMDD) 추출 후 최신 선택
    def _date_key(p: Path) -> str:
        m = re.search(r"(\d{8})", p.name)
        return m.group(1) if m else "00000000"
    return max(candidates, key=_date_key)


def _load_csv(path: Path) -> pd.DataFrame:
    for enc in ("euc-kr", "cp949", "utf-8-sig", "utf-8"):
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str)
            print(f"  인코딩: {enc}")
            return df
        except (UnicodeDecodeError, Exception):
            continue
    raise ValueError(f"CSV 인코딩 감지 실패: {path}")


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    # 부분 매칭
    for col in df.columns:
        for c in candidates:
            if c in col:
                return col
    return None


def _parse_kci_csv(df: pd.DataFrame) -> list[dict[str, Any]]:
    df.columns = [c.strip() for c in df.columns]

    col_title    = _find_column(df, ["학술지명", "저널명", "journal"])
    col_p_issn   = _find_column(df, ["종이식", "p-issn", "pissn", "print"])
    col_e_issn   = _find_column(df, ["전자식", "e-issn", "eissn", "online"])
    col_status   = _find_column(df, ["등재구분", "등재 구분", "구분"])
    col_field    = _find_column(df, ["연구분야", "분야"])

    if not col_title:
        raise ValueError(f"학술지명 컬럼을 찾을 수 없음. 컬럼 목록: {df.columns.tolist()}")

    records = []
    for _, row in df.iterrows():
        title = str(row.get(col_title, "") or "").strip()
        if not title or title.lower() == "nan":
            continue

        p_issn = _normalize_issn(row.get(col_p_issn) if col_p_issn else None)
        e_issn = _normalize_issn(row.get(col_e_issn) if col_e_issn else None)

        if not p_issn and not e_issn:
            continue  # ISSN 없으면 매칭 불가 → 건너뜀

        raw_status = str(row.get(col_status, "") or "").strip() if col_status else ""
        kci_status = KCI_STATUS_MAP.get(raw_status)

        issn_combined = list({x for x in [p_issn, e_issn] if x})

        records.append({
            "title": title[:500],
            "p_issn": p_issn,
            "e_issn": e_issn,
            "issn_combined": issn_combined,
            "kci_status": kci_status,
            "field": str(row.get(col_field, "") or "").strip()[:200] if col_field else None,
        })

    return records


async def _upsert_kci_journals(records: list[dict[str, Any]]) -> dict[str, int]:
    inserted = updated = skipped = 0

    async with AsyncSessionLocal() as session:
        for rec in records:
            p_issn = rec["p_issn"]
            e_issn = rec["e_issn"]

            # p_issn OR e_issn 기준으로 기존 row 탐색
            conditions = []
            if p_issn:
                conditions.append(Journal.p_issn == p_issn)
            if e_issn:
                conditions.append(Journal.e_issn == e_issn)
            # issn ARRAY 안에도 검색 (SJR 기존 row 역추정 백필 전 대비)
            if p_issn:
                conditions.append(Journal.issn.any(p_issn))
            if e_issn:
                conditions.append(Journal.issn.any(e_issn))

            result = await session.execute(
                select(Journal).where(or_(*conditions)).limit(1)
            )
            existing = result.scalar_one_or_none()

            now = datetime.now(timezone.utc)

            if existing:
                # 기존 row 업데이트 (KCI 필드만)
                if p_issn and not existing.p_issn:
                    existing.p_issn = p_issn
                if e_issn and not existing.e_issn:
                    existing.e_issn = e_issn
                if rec["kci_status"]:
                    existing.kci_status = rec["kci_status"]
                existing.kci_indexed = True
                # issn ARRAY 보강
                current_issn = set(existing.issn or [])
                current_issn.update(rec["issn_combined"])
                existing.issn = sorted(current_issn)
                existing.updated_at = now
                updated += 1
            else:
                # 신규 KCI 전용 row (sjr_sourceid=NULL)
                new_journal = Journal(
                    sjr_sourceid=None,
                    title=rec["title"],
                    p_issn=p_issn,
                    e_issn=e_issn,
                    issn=rec["issn_combined"] or None,
                    kci_status=rec["kci_status"],
                    kci_indexed=True,
                    sci_indexed=False,
                    updated_at=now,
                )
                session.add(new_journal)
                inserted += 1

            if (inserted + updated + skipped) % 200 == 0:
                await session.commit()
                print(f"  진행: inserted={inserted} updated={updated} skipped={skipped}")

        await session.commit()

    return {"inserted": inserted, "updated": updated, "skipped": skipped}


async def _verify(jako_journals: list[str]) -> None:
    async with AsyncSessionLocal() as session:
        total_kci = (
            await session.execute(
                select(func.count()).select_from(Journal).where(Journal.kci_indexed.is_(True))
            )
        ).scalar()
        print(f"\n[검증] KCI 등재 저널 수: {total_kci:,}개")

        status_rows = (
            await session.execute(
                select(Journal.kci_status, func.count())
                .where(Journal.kci_indexed.is_(True))
                .group_by(Journal.kci_status)
                .order_by(Journal.kci_status)
            )
        ).all()
        print("등재구분 분포:")
        for status, cnt in status_rows:
            print(f"  {status or 'NULL':>12}: {cnt:,}개")

        # JAKO 저널 매칭률 확인
        if jako_journals:
            matched = 0
            for jname in jako_journals:
                r = await session.execute(
                    select(Journal).where(
                        func.lower(Journal.title) == jname.casefold()
                    ).limit(1)
                )
                if r.scalar_one_or_none():
                    matched += 1
            print(f"\nJAKO 저널 KCI 매칭률: {matched}/{len(jako_journals)}개")


def main() -> None:
    parser = argparse.ArgumentParser(description="KCI 등재지 마스터 CSV → journals 테이블 적재")
    parser.add_argument("--dir", default="data/kci_raw", help="CSV 폴더")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_dir = (PROJECT_ROOT / args.dir).resolve()
    csv_path = _find_latest_csv(data_dir)

    if not csv_path:
        print(f"[오류] {data_dir} 에서 KCI CSV를 찾을 수 없습니다.")
        print("  한국연구재단 KCI 포털에서 '학술지정보' CSV를 다운로드 후")
        print(f"  {data_dir}/ 에 저장하세요.")
        sys.exit(1)

    print(f"[입력] {csv_path.name}")
    df = _load_csv(csv_path)
    print(f"  총 {len(df):,}행, 컬럼: {df.columns.tolist()}")

    records = _parse_kci_csv(df)
    print(f"  유효 레코드(ISSN 있음): {len(records):,}개")

    if args.dry_run:
        print("\n[dry-run] DB 쓰기 생략")
        for r in records[:5]:
            print(f"  {r}")
        return

    # JAKO 저널명 목록 (검증용)
    import json
    parsed_path = PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"
    jako_journals: list[str] = []
    if parsed_path.exists():
        data = json.loads(parsed_path.read_text(encoding="utf-8-sig"))
        jako_journals = list({
            p["JournalName"]
            for p in data.get("papers", [])
            if p.get("DBCode") == "JAKO" and p.get("JournalName")
        })

    async def _run() -> None:
        stats = await _upsert_kci_journals(records)
        print(f"\n[완료] inserted={stats['inserted']:,} updated={stats['updated']:,} skipped={stats['skipped']:,}")
        await _verify(jako_journals)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
