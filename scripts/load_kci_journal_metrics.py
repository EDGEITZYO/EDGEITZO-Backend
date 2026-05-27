"""KCI citationDetail → 저널 시계열 IF/SJR 적재 (MVP 외, 향후 사용 대비).

대상: JAKO 138개가 속한 고유 저널 (~62개)
저장: Postgres journal_metrics 테이블 (연도별 row)

체크포인트: data/checkpoints/kci_journal_metrics.json
미매칭:     data/unmatched/kci_journal_metrics.json

사용법:
  python scripts/load_kci_journal_metrics.py
  python scripts/load_kci_journal_metrics.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import xmltodict

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

from sqlalchemy import Column, Float, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.models.base import Base
from app.models.journal import Journal
from sqlalchemy import select

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "kci_journal_metrics.json"
UNMATCHED_PATH  = PROJECT_ROOT / "data" / "unmatched"   / "kci_journal_metrics.json"
PARSED_PATH     = PROJECT_ROOT / "data" / "parsed"      / "scienceon_keywords_normalized.json"

RATE_LIMIT_DELAY = 0.12
MAX_RETRIES      = 3


# ---------------------------------------------------------------------------
# JournalMetric 모델 (인라인 정의 — 별도 migration 없이 create_all로 생성)
# ---------------------------------------------------------------------------

class JournalMetric(Base):
    __tablename__ = "journal_metrics"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    journal_id = Column(Integer, nullable=False, index=True)
    year       = Column(Integer, nullable=False)
    if_1yr     = Column(Float, nullable=True)
    if_3yr     = Column(Float, nullable=True)
    if_4yr     = Column(Float, nullable=True)
    if_5yr     = Column(Float, nullable=True)
    wos_if     = Column(Float, nullable=True)
    sjr        = Column(Float, nullable=True)
    immediacy  = Column(Float, nullable=True)
    self_cited_rate = Column(Float, nullable=True)
    kci_registration = Column(String(20), nullable=True)

    __table_args__ = (
        UniqueConstraint("journal_id", "year", name="uq_journal_metrics_journal_year"),
    )


# ---------------------------------------------------------------------------
# KCI API
# ---------------------------------------------------------------------------

def _kci_params(api_code: str, **kwargs) -> dict:
    return {"apiCode": api_code, "key": settings.kci_api_key, **kwargs}


async def _get_xml(client: httpx.AsyncClient, params: dict) -> dict | None:
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(settings.kci_base_url, params=params, timeout=20.0)
            if r.status_code == 429:
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return xmltodict.parse(r.text)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                print(f"  [에러] {e}")
            await asyncio.sleep(2 ** attempt)
    return None


def _safe_float(val: Any) -> float | None:
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


async def fetch_citation_detail(client: httpx.AsyncClient, journal_id: str) -> tuple[list[dict], str | None]:
    """citationDetail → (연도별 IF/SJR 리스트, kci_registration) 반환."""
    params = _kci_params("citationDetail", id=journal_id)
    parsed = await _get_xml(client, params)
    if not parsed:
        return [], None

    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        j_info = record.get("journalInfo", {})

        kci_reg = None
        reg_node = j_info.get("registration", {}) if isinstance(j_info, dict) else {}
        if isinstance(reg_node, dict):
            kci_reg = reg_node.get("kci-registration")

        cit_history = record.get("journal-citation-index-history", {})
        items = cit_history.get("journal-citation-index") or []
        if isinstance(items, dict):
            items = [items]

        def _pct(val: Any) -> float | None:
            if not val:
                return None
            return _safe_float(str(val).replace("%", "").strip())

        rows = []
        for item in items:
            year_val = item.get("@year") or item.get("year")
            try:
                year = int(year_val) if year_val else None
            except (ValueError, TypeError):
                year = None
            if not year:
                continue

            rows.append({
                "year": year,
                "if_1yr":        _safe_float(item.get("impactFactor")),
                "if_3yr":        _safe_float(item.get("impactFactor3")),
                "if_4yr":        _safe_float(item.get("impactFactor4")),
                "if_5yr":        _safe_float(item.get("impactFactor5")),
                "wos_if":        _safe_float(item.get("wosImpactFactor")),
                "sjr":           _safe_float(item.get("sjr")),
                "immediacy":     _safe_float(item.get("immediacyIndex")),
                "self_cited_rate": _pct(item.get("selfCitedRate")),
                "kci_registration": str(kci_reg).strip() if kci_reg else None,
            })
        return rows, kci_reg
    except Exception as e:
        print(f"  [파싱 오류] citationDetail: {e}")
        return [], None


# ---------------------------------------------------------------------------
# journal-id 조회: articleDetail 재호출 (journalSearch 미지원으로 대체)
# ---------------------------------------------------------------------------

async def get_journal_id_from_article(client: httpx.AsyncClient, art_id: str) -> str | None:
    """articleDetail 응답의 journalInfo @journal-id 추출."""
    params = _kci_params("articleDetail", id=art_id)
    parsed = await _get_xml(client, params)
    if not parsed:
        return None
    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        j_info = record.get("journalInfo", {})
        return j_info.get("@journal-id") or j_info.get("journal-id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 체크포인트
# ---------------------------------------------------------------------------

def _load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 메인 처리
# ---------------------------------------------------------------------------

def _load_jako_journals() -> list[dict]:
    """JAKO 논문에서 고유 저널 목록 추출. cns 필드에 해당 저널의 CN 목록 포함."""
    data = json.loads(PARSED_PATH.read_text(encoding="utf-8-sig"))
    seen: dict[str, dict] = {}
    for p in data.get("papers", []):
        if not p.get("CN", "").startswith("JAKO"):
            continue
        jname = p.get("JournalName", "")
        cn = p.get("CN", "")
        issn_list = p.get("ISSN") or []
        if isinstance(issn_list, str):
            issn_list = [issn_list]
        if not jname:
            continue
        if jname not in seen:
            seen[jname] = {"name": jname, "issns": issn_list, "cns": [cn]}
        else:
            if not seen[jname]["issns"] and issn_list:
                seen[jname]["issns"] = issn_list
            if cn and cn not in seen[jname]["cns"]:
                seen[jname]["cns"].append(cn)
    return list(seen.values())


def _load_citations_art_ids() -> dict[str, str]:
    """citations 체크포인트에서 CN → art_id 매핑 반환 (status=ok만)."""
    path = PROJECT_ROOT / "data" / "checkpoints" / "kci_citations.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {cn: v["art_id"] for cn, v in data.items() if v.get("status") == "ok" and v.get("art_id")}


async def process_journals(journals: list[dict], *, dry_run: bool) -> None:
    checkpoint = _load_checkpoint()
    citations_art_ids = _load_citations_art_ids()
    unmatched: list[dict] = []
    success = failed = skipped = 0

    print(f"  citations 체크포인트: {len(citations_art_ids)}개 art_id 로드됨")

    # journal_metrics 테이블 생성 (없는 경우)
    if not dry_run:
        from app.core.database import engine
        async with engine.begin() as conn:
            await conn.run_sync(JournalMetric.__table__.create, checkfirst=True)

    async with httpx.AsyncClient() as client:
        for journal in journals:
            jname = journal["name"]
            issns = journal["issns"]
            cns = journal.get("cns", [])

            if jname in checkpoint:
                skipped += 1
                continue

            await asyncio.sleep(RATE_LIMIT_DELAY)

            # DB에서 journal_id 조회 (ISSN → title 순서)
            journal_db_id = None
            async with AsyncSessionLocal() as session:
                for issn in issns:
                    r = await session.execute(
                        select(Journal).where(
                            Journal.issn.any(issn.replace("-", ""))
                        ).limit(1)
                    )
                    j = r.scalar_one_or_none()
                    if j:
                        journal_db_id = j.id
                        break
                if not journal_db_id:
                    r = await session.execute(
                        select(Journal).where(
                            Journal.title.ilike(f"%{jname[:30]}%")
                        ).limit(1)
                    )
                    j = r.scalar_one_or_none()
                    if j:
                        journal_db_id = j.id

            if not journal_db_id:
                print(f"  [미매칭] '{jname}' — journals 테이블에 없음")
                unmatched.append({"name": jname, "issns": issns, "reason": "not in journals table"})
                checkpoint[jname] = {"status": "unmatched"}
                failed += 1
                _save_checkpoint(checkpoint)
                continue

            # citations 체크포인트에서 art_id 조회 (저널 소속 CN 중 매칭된 것)
            art_id = None
            for cn in cns:
                if cn in citations_art_ids:
                    art_id = citations_art_ids[cn]
                    break

            if not art_id:
                print(f"  [미매칭] '{jname}' — citations 체크포인트에 art_id 없음 (CN: {cns[:2]})")
                unmatched.append({"name": jname, "issns": issns, "cns": cns, "reason": "no art_id in citations checkpoint"})
                checkpoint[jname] = {"status": "no_art_id"}
                failed += 1
                _save_checkpoint(checkpoint)
                continue

            # articleDetail → journalInfo @journal-id 추출
            await asyncio.sleep(RATE_LIMIT_DELAY)
            kci_journal_id = await get_journal_id_from_article(client, art_id)

            if not kci_journal_id:
                print(f"  [미매칭] '{jname}' — KCI journal-id 없음 (art_id={art_id})")
                unmatched.append({"name": jname, "art_id": art_id, "reason": "kci journal-id not found"})
                checkpoint[jname] = {"status": "no_kci_id", "art_id": art_id}
                failed += 1
                _save_checkpoint(checkpoint)
                continue

            # citationDetail → 연도별 IF/SJR 시계열
            await asyncio.sleep(RATE_LIMIT_DELAY)
            rows, _ = await fetch_citation_detail(client, kci_journal_id)
            print(f"  '{jname[:40]}' journal_id={journal_db_id} kci_id={kci_journal_id} → {len(rows)}년치")

            if not dry_run and rows:
                async with AsyncSessionLocal() as session:
                    for row in rows:
                        stmt = pg_insert(JournalMetric).values(
                            journal_id=journal_db_id,
                            **row,
                        ).on_conflict_do_update(
                            constraint="uq_journal_metrics_journal_year",
                            set_={k: row[k] for k in row if k != "year"},
                        )
                        await session.execute(stmt)
                    await session.commit()

            checkpoint[jname] = {
                "status": "ok",
                "kci_journal_id": kci_journal_id,
                "art_id": art_id,
                "years": len(rows),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_checkpoint(checkpoint)
            success += 1

    print(f"\n[완료] 성공={success} 미매칭={failed} 스킵(기존)={skipped}")
    if unmatched:
        UNMATCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNMATCHED_PATH.write_text(json.dumps(unmatched, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  미매칭: {UNMATCHED_PATH} ({len(unmatched)}건)")


def main() -> None:
    parser = argparse.ArgumentParser(description="KCI 저널 시계열 IF 적재")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset-checkpoint", action="store_true")
    args = parser.parse_args()

    if not settings.kci_api_key:
        print("[오류] KCI_API_KEY가 .env에 설정되지 않았습니다.")
        sys.exit(1)

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[초기화] 체크포인트 삭제")

    journals = _load_jako_journals()
    print(f"=== KCI 저널 시계열 적재 시작: {len(journals)}개 저널 ===")

    asyncio.run(process_journals(journals, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
