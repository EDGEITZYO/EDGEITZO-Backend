"""KCI articleDetail.referenceInfo 전체 재수집 → 코퍼스 밖 참고문헌(backward) 메타데이터 적재.

scripts/load_paper_citations_from_kci.py는 참고문헌 중 자체 코퍼스 안에 있는 것만 Neo4j CITES
엣지로 반영하고, 코퍼스 밖 항목은 @arti-id만 확인한 뒤 버렸다. 이 스크립트는 그 버려진 항목들을
title/author/journal-name/pubi-year/doi까지 포함해 paper_citation_external_refs 테이블에 저장한다
— 인용관계 그래프에서 "상세페이지로 이동은 못 하지만 정보는 보이는" 노드로 쓰기 위함.

이미 코퍼스 안에 있는(=papers.kci_art_id에 존재하는) 참고문헌은 Neo4j CITES가 이미 담당하므로
여기서는 저장하지 않는다(중복 방지).

체크포인트: data/checkpoints/paper_citation_external_refs_kci.json (source_cn 단위 재시작 가능)

사용법:
  python scripts/load_paper_citation_external_refs_kci.py --dry-run --limit 20
  python scripts/load_paper_citation_external_refs_kci.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
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

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.models.paper import PaperCitationExternalRef

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "paper_citation_external_refs_kci.json"
RATE_LIMIT_DELAY = 0.35
MAX_RETRIES = 4


def _kci_params(api_code: str, **kwargs) -> dict:
    return {"apiCode": api_code, "key": settings.kci_api_key, **kwargs}


async def _get_xml(client: httpx.AsyncClient, params: dict, *, retries: int = MAX_RETRIES) -> dict | None:
    for attempt in range(retries):
        try:
            r = await client.get(settings.kci_base_url, params=params, timeout=20.0)
            if r.status_code in (429, 503):
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            if r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return xmltodict.parse(r.text)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [에러] {e}")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


def _as_list(node: Any) -> list:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _split_authors(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    names = [n.strip() for n in raw.split(";") if n.strip()]
    return names or None


def _safe_int(val: Any) -> int | None:
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


async def fetch_references(client: httpx.AsyncClient, art_id: str) -> list[dict]:
    """articleDetail을 호출해 referenceInfo.reference[] 전체를 파싱 (arti-id 유무 무관)."""
    parsed = await _get_xml(client, _kci_params("articleDetail", id=art_id))
    if not parsed:
        return []

    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        reference_info = record.get("referenceInfo")
        if not reference_info:
            return []
        refs = _as_list(reference_info.get("reference"))

        results = []
        for r in refs:
            if not isinstance(r, dict):
                continue
            arti_id = r.get("@arti-id")
            refebibl_id = r.get("@refebibl-id")
            external_id = arti_id or refebibl_id
            if not external_id:
                continue
            results.append({
                "external_id": external_id,
                "arti_id": arti_id,
                "title": r.get("title"),
                "authors": _split_authors(r.get("author")),
                "journal": r.get("journal-name"),
                "doi": r.get("doi"),
                "pubyear": _safe_int(r.get("pubi-year")),
            })
        return results
    except Exception as e:
        print(f"  [파싱 오류] {art_id}: {e}")
        return []


def _load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict[str, Any]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _load_targets(limit: int | None) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id, kci_art_id FROM papers WHERE kci_art_id IS NOT NULL")
        )
        rows = [(r.id, r.kci_art_id) for r in result.fetchall()]
    if limit:
        rows = rows[:limit]
    return rows


async def _load_own_art_ids() -> set[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT kci_art_id FROM papers WHERE kci_art_id IS NOT NULL"))
        return {r[0] for r in result.fetchall()}


_INSERT_BATCH_SIZE = 1000  # 컬럼 10개 * 1000 = 10,000 파라미터 (Postgres 한도 32,767 여유있게 하회)


async def _insert_external_refs(rows: list[dict]) -> int:
    if not rows:
        return 0
    inserted = 0
    async with AsyncSessionLocal() as session:
        for i in range(0, len(rows), _INSERT_BATCH_SIZE):
            batch = rows[i : i + _INSERT_BATCH_SIZE]
            stmt = pg_insert(PaperCitationExternalRef).values(batch).on_conflict_do_nothing(
                index_elements=["source_cn", "direction", "external_id"]
            )
            result = await session.execute(stmt)
            inserted += result.rowcount or 0
        await session.commit()
    return inserted


async def main() -> None:
    parser = argparse.ArgumentParser(description="KCI referenceInfo 전체 → 코퍼스 밖 참고문헌 메타데이터 적재")
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 생략, 수집/집계만 수행")
    parser.add_argument("--limit", type=int, default=None, help="처리할 논문 수 제한 (테스트용)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="체크포인트 초기화 후 전체 재처리")
    args = parser.parse_args()

    if not settings.kci_api_key:
        print("[오류] KCI_API_KEY가 .env에 설정되지 않았습니다.")
        sys.exit(1)

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[초기화] 체크포인트 삭제")

    targets = await _load_targets(args.limit)
    own_art_ids = await _load_own_art_ids()
    print(f"=== 대상 논문 {len(targets)}건 | 코퍼스 내부 art_id {len(own_art_ids)}건 ===")

    checkpoint = _load_checkpoint()
    total_refs = 0
    total_external = 0
    total_inserted = 0
    preview_rows: list[dict] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, (cn, art_id) in enumerate(targets):
            if cn in checkpoint:
                continue

            await asyncio.sleep(RATE_LIMIT_DELAY)
            refs = await fetch_references(client, art_id)
            total_refs += len(refs)

            external_refs = [r for r in refs if r["arti_id"] not in own_art_ids]
            total_external += len(external_refs)

            rows_for_cn = [
                {
                    "source_cn": cn,
                    "direction": "reference",
                    "external_source": "kci",
                    "external_id": r["external_id"],
                    "title": r["title"],
                    "authors": r["authors"],
                    "journal": r["journal"],
                    "doi": r["doi"],
                    "pubyear": r["pubyear"],
                }
                for r in external_refs
            ]

            print(f"  [{i + 1}/{len(targets)}] {cn} refs={len(refs)} external={len(external_refs)}")

            if args.dry_run:
                preview_rows.extend(rows_for_cn)
                checkpoint[cn] = {"refs": len(refs), "external": len(external_refs)}
                _save_checkpoint(checkpoint)
                continue

            # 이 논문의 행을 DB에 확실히 반영한 뒤에만 체크포인트에 기록 — 중간에 죽어도
            # "체크포인트엔 있는데 실제 데이터는 없는" 상태가 되지 않도록 보장.
            total_inserted += await _insert_external_refs(rows_for_cn)
            checkpoint[cn] = {"refs": len(refs), "external": len(external_refs)}
            _save_checkpoint(checkpoint)

    print(f"\n[수집 완료] 총 참고문헌 {total_refs}건, 코퍼스 밖 {total_external}건")

    if args.dry_run:
        print("[dry-run] DB 쓰기 생략")
        for row in preview_rows[:20]:
            print(f"  {row['source_cn']} -> {row['external_id']} | {row['title']}")
        return

    print(f"[Postgres] paper_citation_external_refs {total_inserted}건 insert 완료 (중복은 자동 스킵)")


if __name__ == "__main__":
    asyncio.run(main())
