"""ScienceON browse API → DIKO 학위논문 메타 보강.

browse API(action=browse)로 DIKO CN별 아래 4개 필드를 가져와 저장:
  - Degree      : 학위구분 (국내석사/국내박사)
  - Affiliation : 저자소속기관(학위수여기관)
  - Publisher   : 발행기관(학위수여기관)
  - FulltextFlag: 원문공개 여부 (0/1 → bool)

저장:
  - Neo4j Paper 노드: degree, affiliation, publisher, fulltext_flag 속성
  - Postgres papers: degree, affiliation, publisher, fulltext_flag 컬럼

체크포인트: data/checkpoints/diko_thesis_meta.json
미매칭:     data/unmatched/diko_thesis_meta.json

사용법:
  python scripts/load_diko_thesis_meta.py
  python scripts/load_diko_thesis_meta.py --dry-run
  python scripts/load_diko_thesis_meta.py --limit 10
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

from sqlalchemy import select
from neo4j import GraphDatabase

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.models.paper import Paper

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "diko_thesis_meta.json"
UNMATCHED_PATH  = PROJECT_ROOT / "data" / "unmatched"   / "diko_thesis_meta.json"
PARSED_PATH     = PROJECT_ROOT / "data" / "parsed"      / "scienceon_enriched.json"

RATE_LIMIT_DELAY = 0.15
MAX_RETRIES      = 3


# ---------------------------------------------------------------------------
# browse API
# ---------------------------------------------------------------------------

async def _browse(client: httpx.AsyncClient, cn: str) -> dict | None:
    params = {
        "client_id": settings.scienceon_client_id,
        "token":     settings.scienceon_token,
        "version":   settings.scienceon_version,
        "action":    "browse",
        "target":    "ARTI",
        "cn":        cn,
    }
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.get(settings.scienceon_base_url, params=params, timeout=20.0)
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
                print(f"  [에러] {cn}: {e}")
            await asyncio.sleep(2 ** attempt)
    return None


def _normalize_degree(raw: str | None) -> str | None:
    """ScienceON Degree 값 → 정규화. '국내박사'/'박사' → '박사', '국내석사'/'석사' → '석사', 그 외 → None."""
    if not raw:
        return None
    s = raw.strip()
    if "박사" in s:
        return "박사"
    if "석사" in s:
        return "석사"
    return None


def _extract_meta(parsed: dict) -> dict | None:
    """browse 응답에서 메타 4개 필드 추출."""
    try:
        status = parsed["MetaData"]["resultSummary"]["statusCode"]
        if str(status) != "200":
            return None
        record = parsed["MetaData"]["recordList"]["record"]
    except (KeyError, TypeError):
        return None

    items = record.get("item", [])
    if isinstance(items, dict):
        items = [items]

    flat: dict[str, str | None] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("@metaCode")
        val  = item.get("#text")
        if code:
            flat[code] = val

    degree_raw   = flat.get("Degree")
    affiliation  = flat.get("Affiliation")
    publisher    = flat.get("Publisher")
    fulltext_raw = flat.get("FulltextFlag")

    fulltext_flag: bool | None = None
    if fulltext_raw is not None:
        try:
            fulltext_flag = bool(int(fulltext_raw))
        except (ValueError, TypeError):
            pass

    degree = _normalize_degree(degree_raw)

    return {
        "degree":       degree,
        "affiliation":  affiliation.strip() if affiliation else None,
        "publisher":    publisher.strip() if publisher else None,
        "fulltext_flag": fulltext_flag,
    }


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------

def _neo4j_update(driver, cn: str, meta: dict) -> None:
    sets = []
    params: dict[str, Any] = {"cn": cn}

    field_map = {
        "degree":       "p.degree = $degree",
        "affiliation":  "p.affiliation = $affiliation",
        "publisher":    "p.publisher = $publisher",
        "fulltext_flag": "p.fulltext_flag = $fulltext_flag",
    }
    for key, clause in field_map.items():
        if meta.get(key) is not None:
            sets.append(clause)
            params[key] = meta[key]

    if not sets:
        return

    query = f"MATCH (p:Paper {{cn: $cn}}) SET {', '.join(sets)}"
    with driver.session() as session:
        session.run(query, **params)


async def _postgres_update(cn: str, meta: dict) -> None:
    updates: dict[str, Any] = {
        k: v for k, v in meta.items() if v is not None
    }
    if not updates:
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Paper).where(Paper.scienceon_cn == cn).limit(1)
        )
        paper = result.scalar_one_or_none()
        if paper:
            for k, v in updates.items():
                setattr(paper, k, v)
            paper.updated_at = datetime.now(timezone.utc)
            await session.commit()


# ---------------------------------------------------------------------------
# 체크포인트
# ---------------------------------------------------------------------------

def _load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 메인 처리
# ---------------------------------------------------------------------------

def _load_diko_cns(limit: int | None) -> list[str]:
    data = json.loads(PARSED_PATH.read_text(encoding="utf-8-sig"))
    cns = [
        p["CN"] for p in data.get("papers", [])
        if str(p.get("CN", "")).startswith("DIKO")
    ]
    if limit:
        cns = cns[:limit]
    return cns


async def process(cns: list[str], *, dry_run: bool, driver) -> None:
    checkpoint = _load_checkpoint()
    unmatched: list[dict] = []
    success = failed = skipped = 0

    async with httpx.AsyncClient() as client:
        for i, cn in enumerate(cns):
            if cn in checkpoint:
                skipped += 1
                continue

            await asyncio.sleep(RATE_LIMIT_DELAY)

            parsed = await _browse(client, cn)
            if not parsed:
                print(f"  [실패] {cn} — API 응답 없음")
                unmatched.append({"cn": cn, "reason": "browse failed"})
                checkpoint[cn] = {"status": "failed"}
                failed += 1
                _save_checkpoint(checkpoint)
                continue

            meta = _extract_meta(parsed)
            if not meta:
                print(f"  [실패] {cn} — 메타 파싱 실패")
                unmatched.append({"cn": cn, "reason": "meta parse failed"})
                checkpoint[cn] = {"status": "parse_failed"}
                failed += 1
                _save_checkpoint(checkpoint)
                continue

            print(
                f"  [{i+1}/{len(cns)}] {cn} "
                f"degree={meta['degree']} affil={str(meta['affiliation'])[:20]} "
                f"fulltext={meta['fulltext_flag']}"
            )

            if not dry_run:
                _neo4j_update(driver, cn, meta)
                await _postgres_update(cn, meta)

            checkpoint[cn] = {
                "status": "ok",
                "degree": meta["degree"],
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_checkpoint(checkpoint)
            success += 1

    print(f"\n[완료] 성공={success} 실패={failed} 스킵(기존)={skipped}")
    if unmatched:
        UNMATCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNMATCHED_PATH.write_text(
            json.dumps(unmatched, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  미매칭: {UNMATCHED_PATH} ({len(unmatched)}건)")


def main() -> None:
    parser = argparse.ArgumentParser(description="DIKO 학위논문 메타 보강")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="처리할 CN 수 제한 (테스트용)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="체크포인트 초기화")
    args = parser.parse_args()

    if not settings.scienceon_client_id or not settings.scienceon_token:
        print("[오류] SCIENCEON_CLIENT_ID 또는 SCIENCEON_TOKEN이 .env에 없습니다.")
        sys.exit(1)

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[초기화] 체크포인트 삭제")

    cns = _load_diko_cns(args.limit)
    print(f"=== DIKO 학위논문 메타 보강 시작: {len(cns)}개 ===")
    if args.dry_run:
        print("[dry-run] Neo4j/Postgres 쓰기 생략\n")

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        asyncio.run(process(cns, dry_run=args.dry_run, driver=driver))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
