"""공유 Neo4j(Aura)는 이미 국내 코퍼스로 바뀌었는데 이 환경의 Postgres는 옛 상태일 때 맞춘다.

Neo4j Aura는 로컬·운영이 같은 인스턴스를 쓴다. 한 환경에서 코퍼스 전환
(remove_foreign_papers → load_domestic_reference_papers → add_domestic_corpus_papers)을 돌리면
Neo4j는 모든 환경에 반영되지만 Postgres·Chroma·코퍼스 파일은 그 환경에만 반영된다.
다른 환경은 이 순서로 맞춘다:

  1. python scripts/remove_foreign_papers.py --apply --skip-neo4j
  2. 코퍼스 파일 복사: data/parsed/scienceon_keywords_normalized.json, abstract_sentence_embeddings.pkl
  3. python scripts/sync_domestic_corpus.py            ← 이 스크립트
  4. python scripts/embed_papers.py --skip-existing && python scripts/embed_keywords.py --skip-existing
     (임베딩을 다른 환경 Chroma에서 복사해 왔다면 생략)
  5. 서버 재시작

이 스크립트가 하는 것 (Neo4j는 읽기만 한다):
  - 코퍼스 파일에 있는데 papers에 없는 논문을 KCI에서 받아 적재하고 source='knowledge_base'로 표시
  - Neo4j에서 CITES로 이미 연결된 참고문헌이 external_refs에도 남아 있으면 그 행을 지운다
    ("external_refs에는 그래프 노드가 아닌 대상만" 불변식 — 안 지우면 같은 논문이 두 번 셀 수 있다)

사용법:
  python scripts/sync_domestic_corpus.py --dry-run
  python scripts/sync_domestic_corpus.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
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

import httpx
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.neo4j_client import get_neo4j_driver
from app.services.chroma_search_service import _PAPERS_PATH
from app.services.domestic_paper_service import materialize_domestic_paper, resolve_papers

CORPUS_SOURCE = "knowledge_base"
CALL_PACING_SECONDS = 0.3


def _cites_pairs(sources: list[str]) -> set[tuple[str, str]]:
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            records = session.run(
                "MATCH (a:Paper)-[:CITES]->(b:Paper) WHERE a.cn IN $sources RETURN a.cn AS a, b.cn AS b",
                sources=sources,
            )
            return {(r["a"], r["b"]) for r in records}
    finally:
        driver.close()


async def sync_corpus_papers(corpus_cns: list[str], dry_run: bool) -> None:
    async with AsyncSessionLocal() as session:
        present = set(await resolve_papers(session, corpus_cns))
        missing = [cn for cn in corpus_cns if cn not in present]
        print(f"[코퍼스] {len(corpus_cns)}편 중 papers에 없는 논문 {len(missing)}편")
        if dry_run:
            return
        stats: Counter[str] = Counter()
        async with httpx.AsyncClient(timeout=20.0) as http:
            for cn in missing:
                await asyncio.sleep(CALL_PACING_SECONDS)
                paper_id = await materialize_domestic_paper(session, cn, client=http, retry_failed=True)
                stats["적재" if paper_id == cn else "실패"] += 1
        print(f"[코퍼스] {dict(stats)}")
        result = await session.execute(
            text("UPDATE papers SET source = :s, updated_at = now() WHERE id = ANY(:ids) AND source <> :s"),
            {"s": CORPUS_SOURCE, "ids": corpus_cns},
        )
        await session.commit()
        print(f"[코퍼스] source='{CORPUS_SOURCE}' 표시 {result.rowcount}편")


async def reconcile_external_refs(dry_run: bool) -> None:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, source_cn, direction, external_id FROM paper_citation_external_refs "
                    "WHERE external_id LIKE 'ART%'"
                )
            )
        ).mappings().all()
        targets = await resolve_papers(session, sorted({r["external_id"] for r in rows}))
    sources = sorted({r["source_cn"] for r in rows})
    pairs: set[tuple[str, str]] = set()
    for i in range(0, len(sources), 500):
        pairs |= await asyncio.to_thread(_cites_pairs, sources[i : i + 500])

    stale = []
    for r in rows:
        node = targets[r["external_id"]]["id"] if r["external_id"] in targets else r["external_id"]
        edge = (r["source_cn"], node) if r["direction"] == "reference" else (node, r["source_cn"])
        if edge in pairs:
            stale.append(r["id"])
    print(f"[참고문헌] 국내 논문 행 {len(rows)}개 중 Neo4j CITES와 겹치는 행 {len(stale)}개")
    if dry_run or not stale:
        return
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM paper_citation_external_refs WHERE id = ANY(:ids)"), {"ids": stale})
        await session.commit()
    print(f"[참고문헌] {len(stale)}개 삭제")


async def main() -> None:
    parser = argparse.ArgumentParser(description="공유 Neo4j에 맞춰 이 환경 Postgres를 국내 코퍼스로 동기화")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    corpus = json.loads(_PAPERS_PATH.read_text(encoding="utf-8"))["papers"]
    await sync_corpus_papers([p["CN"] for p in corpus if p.get("CN")], args.dry_run)
    await reconcile_external_refs(args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
