"""OpenAlex cites: 쿼리 재수집(저자 포함) → 코퍼스 밖 피인용(forward) 메타데이터 적재.

scripts/load_paper_citations_from_openalex.py는 피인용 논문 중 자체 코퍼스 안에 있는 것만
Neo4j CITES 엣지로 반영하고, 코퍼스 밖 항목은 버렸다. 이 스크립트는 그 항목들을
title/authors/journal/pubyear/doi까지 포함해 paper_citation_external_refs 테이블에 저장한다.

data/checkpoints/openalex_citations.json(papers.doi -> openalex work id, cited_by_count)을 베이스로,
cited_by_count > 0인 논문만 대상으로 OpenAlex work 목록을 다시 조회한다 — 이번엔 authorships 필드를
포함해서 저자명까지 받는다(기존 openalex_citing_works.json 캐시는 저자가 없어 그대로 못 씀).

체크포인트: data/checkpoints/paper_citation_external_refs_openalex.json (source_cn 단위 재시작 가능)

사용법:
  python scripts/load_paper_citation_external_refs_openalex.py --dry-run --limit 20
  python scripts/load_paper_citation_external_refs_openalex.py
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
from app.models.paper import PaperCitationExternalRef

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "paper_citation_external_refs_openalex.json"
OPENALEX_CITATIONS_PATH = PROJECT_ROOT / "data" / "checkpoints" / "openalex_citations.json"

OPENALEX_MAILTO = "yuri12120771@gmail.com"
RATE_LIMIT_DELAY = 0.15
MAX_RETRIES = 5


def _short_id(openalex_url: str | None) -> str | None:
    if not openalex_url:
        return None
    return openalex_url.rsplit("/", 1)[-1]


def _normalize_doi(doi_url: str | None) -> str | None:
    if not doi_url:
        return None
    doi = doi_url.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if doi.lower().startswith(prefix):
            return doi[len(prefix):]
    return doi


async def fetch_citing_with_authors(client: httpx.AsyncClient, openalex_id: str) -> list[dict] | None:
    """cites:{id} 쿼리 결과를 authorships 포함해서 가져온다. 전체 재시도 실패 시 None."""
    results = []
    cursor = "*"
    select_fields = "id,doi,title,publication_year,authorships"
    while True:
        data = None
        for attempt in range(MAX_RETRIES):
            r = await client.get(
                "https://api.openalex.org/works",
                params={
                    "filter": f"cites:{openalex_id}",
                    "select": select_fields,
                    "per-page": 100,
                    "cursor": cursor,
                    "mailto": OPENALEX_MAILTO,
                },
                timeout=20,
            )
            if r.status_code == 200:
                data = r.json()
                break
            await asyncio.sleep(2 ** (attempt + 1))
        if data is None:
            return None

        for w in data.get("results", []):
            authors = [
                a.get("author", {}).get("display_name")
                for a in w.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]
            results.append({
                "external_id": _short_id(w.get("id")),
                "title": w.get("title"),
                "authors": authors or None,
                "doi": _normalize_doi(w.get("doi")),
                "pubyear": w.get("publication_year"),
            })
        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor or not data.get("results"):
            break

    return results


def _load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict[str, Any]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _load_own_dois() -> set[str]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT doi FROM papers WHERE doi IS NOT NULL"))
        return {_normalize_doi(r[0]) for r in result.fetchall() if r[0]}


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
    parser = argparse.ArgumentParser(description="OpenAlex cites: 재수집(저자 포함) → 코퍼스 밖 피인용 메타데이터 적재")
    parser.add_argument("--dry-run", action="store_true", help="DB 쓰기 생략, 수집/집계만 수행")
    parser.add_argument("--limit", type=int, default=None, help="처리할 논문 수 제한 (테스트용)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="체크포인트 초기화 후 전체 재처리")
    args = parser.parse_args()

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[초기화] 체크포인트 삭제")

    with open(OPENALEX_CITATIONS_PATH, encoding="utf-8") as f:
        oa_citations = json.load(f)

    targets = [
        (cn, v["openalex_id"])
        for cn, v in oa_citations.items()
        if v.get("status") == "ok" and v.get("openalex_id") and (v.get("cited_by_count") or 0) > 0
    ]
    if args.limit:
        targets = targets[: args.limit]

    own_dois = await _load_own_dois()
    print(f"=== 대상 논문 {len(targets)}건 (피인용 1건 이상) | 코퍼스 내부 doi {len(own_dois)}건 ===")

    checkpoint = _load_checkpoint()
    total_citing = 0
    total_external = 0
    total_inserted = 0
    preview_rows: list[dict] = []

    async with httpx.AsyncClient() as client:
        for i, (cn, oa_id) in enumerate(targets):
            if cn in checkpoint:
                continue

            await asyncio.sleep(RATE_LIMIT_DELAY)
            citing = await fetch_citing_with_authors(client, oa_id)
            if citing is None:
                print(f"  [{i + 1}/{len(targets)}] {cn} 재시도 실패 — 다음 실행에 재시도")
                continue

            total_citing += len(citing)
            external = [c for c in citing if c["external_id"] and not (c["doi"] and c["doi"].lower() in own_dois)]
            total_external += len(external)

            rows_for_cn = [
                {
                    "source_cn": cn,
                    "direction": "citing",
                    "external_source": "openalex",
                    "external_id": c["external_id"],
                    "title": c["title"],
                    "authors": c["authors"],
                    "journal": None,
                    "doi": c["doi"],
                    "pubyear": c["pubyear"],
                }
                for c in external
            ]

            print(f"  [{i + 1}/{len(targets)}] {cn} citing={len(citing)} external={len(external)}")

            if args.dry_run:
                preview_rows.extend(rows_for_cn)
                checkpoint[cn] = {"citing": len(citing), "external": len(external)}
                _save_checkpoint(checkpoint)
                continue

            # 이 논문의 행을 DB에 확실히 반영한 뒤에만 체크포인트에 기록 — 중간에 죽어도
            # "체크포인트엔 있는데 실제 데이터는 없는" 상태가 되지 않도록 보장.
            total_inserted += await _insert_external_refs(rows_for_cn)
            checkpoint[cn] = {"citing": len(citing), "external": len(external)}
            _save_checkpoint(checkpoint)

    print(f"\n[수집 완료] 총 피인용 {total_citing}건, 코퍼스 밖 {total_external}건")

    if args.dry_run:
        print("[dry-run] DB 쓰기 생략")
        for row in preview_rows[:20]:
            print(f"  {row['source_cn']} <- {row['external_id']} | {row['title']}")
        return

    print(f"[Postgres] paper_citation_external_refs {total_inserted}건 insert 완료 (중복은 자동 스킵)")


if __name__ == "__main__":
    asyncio.run(main())
