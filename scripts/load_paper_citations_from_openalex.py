"""OpenAlex referenced_works → 자체 코퍼스 내부 CITES 엣지 추가 적재.

scripts/load_paper_citations_from_kci.py(KCI referenceInfo 기반)와 같은 목적(자체 950건
코퍼스 내부의 실제 인용관계만 반영)이지만, 소스가 다르다:

  - KCI 소스는 국내 학술논문(JAKO)만 커버하고, CN↔KCI art_id 매칭에 의존한다.
  - OpenAlex는 DOI만 있으면 국내/해외(JAKO/JAFO) 가리지 않고 조회되고, API 키도 필요 없다.
    (실측: 무작위 20건 중 DOI 매칭 14건, 그중 7건이 실제 피인용 데이터 보유 — KCI 단독 대비 크게 넓음)

주의: 이 스크립트는 "상세페이지로 이동 가능한, 코퍼스 내부 논문끼리의 엣지"만 추가한다.
OpenAlex가 찾아주는 피인용/참고문헌 중 절대다수는 코퍼스 밖(국제 학술지) 논문이라 이 범위에서
자동으로 제외된다 — 코퍼스 밖 논문을 "정보용(비-클릭) 노드"로 보여주는 기능은 별도 논의 후
진행하기로 하고 이번 스크립트에는 포함하지 않는다.

흐름:
  1. papers.doi IS NOT NULL 인 논문 전체를 Postgres에서 로드
  2. 각 논문의 DOI로 OpenAlex work를 조회 (referenced_works 포함) — 체크포인트로 재시작 가능
  3. 전체 수집 후 own_openalex_id → cn 역인덱스 구성
  4. 각 논문의 referenced_works 중 own_openalex_id에 있는 것만 edge로 채택
     (한 방향만 수집하면 충분 — 모든 논문을 "citing" 관점으로 훑으므로 상대방 논문이 우리
     코퍼스 안에 있는 한 반대 방향도 자연히 다른 논문 처리 시점에 커버됨)
  5. Neo4j (:Paper {cn: citing})-[:CITES]->(:Paper {cn: cited}) MERGE
     (scripts/load_kci_citations_from_kci.py가 이미 넣은 엣지와 겹치면 MERGE라 중복 없이 스킵됨)

체크포인트: data/checkpoints/openalex_citations.json

사용법:
  python scripts/load_paper_citations_from_openalex.py --dry-run --limit 20
  python scripts/load_paper_citations_from_openalex.py
  python scripts/load_paper_citations_from_openalex.py --reset-checkpoint
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

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "openalex_citations.json"

OPENALEX_BASE_URL = "https://api.openalex.org/works"
OPENALEX_MAILTO = "yuri12120771@gmail.com"  # OpenAlex "polite pool" — 더 높은 레이트리밋 + 예의상 명시
RATE_LIMIT_DELAY = 0.15
MAX_RETRIES = 4


def _short_id(openalex_url: str | None) -> str | None:
    """https://openalex.org/W123 -> W123."""
    if not openalex_url:
        return None
    return openalex_url.rsplit("/", 1)[-1]


async def _fetch_work(client: httpx.AsyncClient, doi: str, *, retries: int = MAX_RETRIES) -> dict | None:
    url = f"{OPENALEX_BASE_URL}/https://doi.org/{doi}"
    for attempt in range(retries):
        try:
            r = await client.get(url, params={"mailto": OPENALEX_MAILTO}, timeout=20.0)
            if r.status_code == 404:
                return {"status": "not_found"}
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** (attempt + 1)
                print(f"  [{r.status_code}] {wait}초 대기")
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            return {
                "status": "ok",
                "openalex_id": _short_id(data.get("id")),
                "referenced_works": [_short_id(w) for w in data.get("referenced_works", []) if w],
                "cited_by_count": data.get("cited_by_count"),
            }
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [에러] {doi}: {e}")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


def _normalize_doi(doi_url: str) -> str:
    doi = doi_url.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if doi.lower().startswith(prefix):
            return doi[len(prefix):]
    return doi


def _load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict[str, Any]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def _load_papers_with_doi(limit: int | None) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT id, doi FROM papers WHERE doi IS NOT NULL"))
        rows = [(r.id, r.doi) for r in result.fetchall()]
    if limit:
        rows = rows[:limit]
    return rows


async def collect(papers: list[tuple[str, str]]) -> dict[str, Any]:
    checkpoint = _load_checkpoint()

    async with httpx.AsyncClient() as client:
        for i, (cn, doi_url) in enumerate(papers):
            if cn in checkpoint:
                continue

            await asyncio.sleep(RATE_LIMIT_DELAY)
            doi = _normalize_doi(doi_url)
            result = await _fetch_work(client, doi)

            if result is None:
                continue  # 재시도 실패 — 다음 실행 시 재시도 (체크포인트에 안 남김)

            checkpoint[cn] = result
            status = result["status"]
            extra = f"refs={len(result.get('referenced_works', []))}" if status == "ok" else ""
            print(f"  [{i + 1}/{len(papers)}] {cn} {status} {extra}")
            _save_checkpoint(checkpoint)

    return checkpoint


def build_edges(checkpoint: dict[str, Any]) -> list[tuple[str, str]]:
    own_id_to_cn = {
        v["openalex_id"]: cn for cn, v in checkpoint.items() if v.get("status") == "ok" and v.get("openalex_id")
    }

    edges: list[tuple[str, str]] = []
    for cn, v in checkpoint.items():
        if v.get("status") != "ok":
            continue
        for ref_id in v.get("referenced_works", []):
            cited_cn = own_id_to_cn.get(ref_id)
            if cited_cn and cited_cn != cn:
                edges.append((cn, cited_cn))
    return edges


def _neo4j_merge_citation(driver, citing_cn: str, cited_cn: str) -> None:
    query = """
    MATCH (a:Paper {cn: $citing_cn})
    MATCH (b:Paper {cn: $cited_cn})
    MERGE (a)-[:CITES]->(b)
    """
    with driver.session() as session:
        session.run(query, citing_cn=citing_cn, cited_cn=cited_cn)


async def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAlex referenced_works → 자체 코퍼스 CITES 엣지 추가 적재")
    parser.add_argument("--dry-run", action="store_true", help="Neo4j 쓰기 생략, 수집/집계만 수행")
    parser.add_argument("--limit", type=int, default=None, help="처리할 논문 수 제한 (테스트용)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="체크포인트 초기화 후 전체 재처리")
    args = parser.parse_args()

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[초기화] 체크포인트 삭제")

    papers = await _load_papers_with_doi(args.limit)
    print(f"=== 대상 논문 {len(papers)}건 (DOI 보유) ===")

    checkpoint = await collect(papers)
    ok_count = sum(1 for v in checkpoint.values() if v.get("status") == "ok")
    print(f"\n[수집 완료] OpenAlex 매칭 {ok_count}/{len(papers)}건")

    edges = build_edges(checkpoint)
    print(f"[집계 완료] 자체 코퍼스 내부 인용 edge {len(edges)}건 (KCI 소스 결과와 별개로 추가 발견분)")

    if args.dry_run:
        print("[dry-run] Neo4j 쓰기 생략")
        for citing, cited in edges[:20]:
            print(f"  {citing} -> {cited}")
        return

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        for citing, cited in edges:
            _neo4j_merge_citation(driver, citing, cited)
        print(f"[Neo4j] CITES 엣지 {len(edges)}건 MERGE 완료 (기존과 중복되는 건 자동으로 스킵됨)")
    finally:
        driver.close()


if __name__ == "__main__":
    asyncio.run(main())
