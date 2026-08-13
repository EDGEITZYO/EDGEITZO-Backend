"""KCI Open API articleDetail.referenceInfo → 자체 코퍼스 내부 CITES 엣지 적재.

흐름:
  data/checkpoints/kci_citations.json (scripts/load_kci_citations.py가 만든 CN→KCI art_id
  매칭 결과, status=="ok"만) 로드
  → art_id 역인덱스(art_id → CN) 구성 (자체 코퍼스 안에서만 유효)
  → 각 CN의 art_id로 articleDetail을 다시 호출, referenceInfo.reference[]에서 @arti-id 추출
  → @arti-id가 역인덱스에 있으면(=참고문헌이 자체 서비스 코퍼스 안에도 있으면) edge로 채택
  → Neo4j (:Paper {cn: citing})-[:CITES]->(:Paper {cn: cited}) MERGE
  → 부수 작업: Postgres papers.kci_art_id 백필 (기존엔 컬럼만 있고 전부 NULL이었음)

주의: KCI Open API는 짧은 시간에 요청이 몰리면 307(redirect)/503을 반환한다(직접 확인함).
따라서 기존 load_kci_citations.py보다 느린 RATE_LIMIT_DELAY를 쓰고, follow_redirects=True와
503 재시도를 반드시 포함한다.

체크포인트: data/checkpoints/kci_citation_edges.json (CN 단위 재시작 가능)

사용법:
  python scripts/load_paper_citations_from_kci.py --dry-run --limit 10
  python scripts/load_paper_citations_from_kci.py
  python scripts/load_paper_citations_from_kci.py --reset-checkpoint
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
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

from neo4j import GraphDatabase
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.settings import settings

KCI_CITATIONS_CHECKPOINT = PROJECT_ROOT / "data" / "checkpoints" / "kci_citations.json"
EDGES_CHECKPOINT = PROJECT_ROOT / "data" / "checkpoints" / "kci_citation_edges.json"

RATE_LIMIT_DELAY = 0.35   # load_kci_citations.py(0.12)보다 보수적으로 — 307/503 재발 방지
MAX_RETRIES = 4


# ---------------------------------------------------------------------------
# KCI API
# ---------------------------------------------------------------------------

def _kci_params(api_code: str, **kwargs) -> dict:
    return {"apiCode": api_code, "key": settings.kci_api_key, **kwargs}


async def _get_xml(client: httpx.AsyncClient, params: dict, *, retries: int = MAX_RETRIES) -> dict | None:
    for attempt in range(retries):
        try:
            r = await client.get(settings.kci_base_url, params=params, timeout=20.0)
            if r.status_code in (429, 503):
                wait = 2 ** (attempt + 1)
                print(f"  [{r.status_code}] {wait}초 대기")
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            parsed = xmltodict.parse(r.text)
            err = _extract_error(parsed)
            if err:
                print(f"  [KCI 오류] {err}")
                if "등록되지 않은 key" in err or "사용기간" in err:
                    sys.exit(f"[치명] KCI API 키 오류 — .env KCI_API_KEY 확인")
                return None
            return parsed
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [에러] {e}")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


def _extract_error(parsed: dict) -> str | None:
    try:
        return parsed.get("error", {}).get("message") or parsed.get("result", {}).get("message")
    except Exception:
        return None


def _as_list(node: Any) -> list:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


async def fetch_reference_arti_ids(client: httpx.AsyncClient, art_id: str) -> list[str]:
    """articleDetail을 호출해 referenceInfo.reference[]에서 @arti-id가 있는 항목만 반환."""
    parsed = await _get_xml(client, _kci_params("articleDetail", id=art_id))
    if not parsed:
        return []

    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        reference_info = record.get("referenceInfo")
        if not reference_info:
            return []
        refs = _as_list(reference_info.get("reference"))
        return [r.get("@arti-id") for r in refs if isinstance(r, dict) and r.get("@arti-id")]
    except Exception as e:
        print(f"  [파싱 오류] articleDetail referenceInfo: {e}")
        return []


# ---------------------------------------------------------------------------
# 체크포인트
# ---------------------------------------------------------------------------

def _load_checkpoint(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Neo4j / Postgres 적재
# ---------------------------------------------------------------------------

def _neo4j_merge_citation(driver, citing_cn: str, cited_cn: str) -> None:
    query = """
    MATCH (a:Paper {cn: $citing_cn})
    MATCH (b:Paper {cn: $cited_cn})
    MERGE (a)-[:CITES]->(b)
    """
    with driver.session() as session:
        session.run(query, citing_cn=citing_cn, cited_cn=cited_cn)


async def _backfill_kci_art_ids(cn_to_art_id: dict[str, str]) -> int:
    updated = 0
    async with AsyncSessionLocal() as session:
        for cn, art_id in cn_to_art_id.items():
            result = await session.execute(
                text("UPDATE papers SET kci_art_id = :art_id WHERE id = :cn AND kci_art_id IS NULL"),
                {"art_id": art_id, "cn": cn},
            )
            updated += result.rowcount or 0
        await session.commit()
    return updated


# ---------------------------------------------------------------------------
# 메인 처리
# ---------------------------------------------------------------------------

async def collect_edges(
    cn_to_art_id: dict[str, str],
    art_id_to_cn: dict[str, str],
    *,
    limit: int | None,
) -> list[tuple[str, str]]:
    checkpoint = _load_checkpoint(EDGES_CHECKPOINT)
    edges: list[tuple[str, str]] = []

    items = list(cn_to_art_id.items())
    if limit:
        items = items[:limit]

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, (cn, art_id) in enumerate(items):
            if cn in checkpoint:
                edges.extend((cn, cited) for cited in checkpoint[cn].get("cited_cns", []))
                continue

            await asyncio.sleep(RATE_LIMIT_DELAY)
            arti_ids = await fetch_reference_arti_ids(client, art_id)
            cited_cns = sorted({art_id_to_cn[a] for a in arti_ids if a in art_id_to_cn and art_id_to_cn[a] != cn})

            print(f"  [{i + 1}/{len(items)}] {cn} art_id={art_id} refs={len(arti_ids)} in_corpus={len(cited_cns)}")

            checkpoint[cn] = {"cited_cns": cited_cns, "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            _save_checkpoint(EDGES_CHECKPOINT, checkpoint)
            edges.extend((cn, cited) for cited in cited_cns)

    return edges


async def main() -> None:
    parser = argparse.ArgumentParser(description="KCI referenceInfo → 자체 코퍼스 CITES 엣지 적재")
    parser.add_argument("--dry-run", action="store_true", help="Neo4j/Postgres 쓰기 생략, 수집만 수행")
    parser.add_argument("--limit", type=int, default=None, help="처리할 논문 수 제한 (테스트용)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="체크포인트 초기화 후 전체 재처리")
    args = parser.parse_args()

    if not settings.kci_api_key:
        print("[오류] KCI_API_KEY가 .env에 설정되지 않았습니다.")
        sys.exit(1)

    if args.reset_checkpoint and EDGES_CHECKPOINT.exists():
        EDGES_CHECKPOINT.unlink()
        print("[초기화] 체크포인트 삭제")

    kci_citations = _load_checkpoint(KCI_CITATIONS_CHECKPOINT)
    cn_to_art_id = {cn: v["art_id"] for cn, v in kci_citations.items() if v.get("status") == "ok"}
    art_id_to_cn = {art_id: cn for cn, art_id in cn_to_art_id.items()}
    print(f"=== 대상 논문 {len(cn_to_art_id)}건 (KCI art_id 매칭 완료분) ===")

    edges = await collect_edges(cn_to_art_id, art_id_to_cn, limit=args.limit)
    print(f"\n[수집 완료] 자체 코퍼스 내부 인용 edge {len(edges)}건")

    if args.dry_run:
        print("[dry-run] Neo4j/Postgres 쓰기 생략")
        for citing, cited in edges[:20]:
            print(f"  {citing} -> {cited}")
        return

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        for citing, cited in edges:
            _neo4j_merge_citation(driver, citing, cited)
        print(f"[Neo4j] CITES 엣지 {len(edges)}건 MERGE 완료")
    finally:
        driver.close()

    updated = await _backfill_kci_art_ids(cn_to_art_id)
    print(f"[Postgres] papers.kci_art_id 백필 {updated}건")


if __name__ == "__main__":
    asyncio.run(main())
