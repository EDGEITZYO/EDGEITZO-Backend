"""코퍼스 논문이 인용한 국내(KCI) 논문을 서비스 논문으로 미리 적재한다 (인용관계 그래프 1단계).

in_service는 이제 "KCI 논문 ID(ART…)가 있는가"로 판정한다(app/services/domestic_paper_service.py).
KCI 논문은 상세페이지나 그래프 확장을 요청하는 순간 KCI에서 받아 적재되므로 끝없이 적재할
필요가 없다. 이 스크립트는 첫 화면에 나오는 1단계 노드만 미리 적재해 첫 클릭을 빠르게 한다.
2단계부터는 실시간 경로가 맡는다. 적재 로직은 실시간 경로와 같은 함수(materialize_domestic_paper)를 쓴다.

처리 순서:
  1. KCI ID 없는 한글 제목 참고문헌(REF…)을 KCI 제목 검색으로 매칭 → 맞으면 external_id를 ART…로 바꾼다
     (KCI가 arti-id를 못 붙인 KCI 논문 구제. 못 맞춘 건 단행본·법령·백서 등이라 그래프에서 제외된다)
  2. 대상 = 검색 코퍼스 논문의 참고문헌 중 ART…(1단계) + 참고문헌을 아직 안 받은 코퍼스·1단계 KCI 노드
  3. 각각 materialize_domestic_paper — papers 적재, Neo4j 노드, 그 논문의 참고문헌 연결,
     external_refs → CITES 전환

검색 코퍼스(JSON/ChromaDB)는 건드리지 않는다.
재실행하면 이미 끝난 논문은 DB 조회만 하고 넘어간다.

사용법:
  python scripts/load_domestic_reference_papers.py --dry-run
  python scripts/load_domestic_reference_papers.py
  python scripts/load_domestic_reference_papers.py --skip-title-match --concurrency 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
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
from app.integrations.kci.researcher_client import KCIResearcherClient
from app.services.chroma_search_service import _PAPERS_PATH
from app.services.domestic_paper_service import materialize_domestic_paper

CALL_PACING_SECONDS = 0.3

_UNMATCHED_KOREAN_REFS_SQL = """
SELECT id, source_cn, direction, external_id, title, pubyear
FROM paper_citation_external_refs
WHERE external_id LIKE 'REF%' AND title ~ '[가-힣]'
ORDER BY id
"""

# 1단계만: 검색 코퍼스 논문의 참고문헌. 적재된 논문의 참고문헌(2단계)까지 대상으로 잡으면
# 재실행할 때마다 한 단계씩 끝없이 넓어진다 — 2단계부터는 실시간 경로가 맡는다.
_ART_REFS_SQL = """
SELECT DISTINCT external_id FROM paper_citation_external_refs
WHERE external_id LIKE 'ART%' AND source_cn = ANY(:corpus)
ORDER BY external_id
"""


def _norm_title(title: str | None) -> str:
    title = unicodedata.normalize("NFKC", title or "").lower()
    return re.sub(r"[\W_]+", "", title)


async def match_korean_refs_by_title(dry_run: bool) -> int:
    """KCI 제목 검색으로 REF… 한글 참고문헌을 KCI 논문에 매칭. 제목(공백·기호 무시)이 완전히 같고
    연도가 있으면 연도까지 같을 때만 인정한다 — 비슷한 제목의 다른 논문에 붙이는 것보다 놓치는 게 낫다."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(_UNMATCHED_KOREAN_REFS_SQL))).mappings().all()
    print(f"[제목 매칭] KCI ID 없는 한글 참고문헌 {len(rows)}건")
    matched = 0
    async with httpx.AsyncClient(timeout=20.0) as http:
        client = KCIResearcherClient(http)
        for row in rows:
            await asyncio.sleep(CALL_PACING_SECONDS)
            try:
                _, articles = await client.search_by_title(row["title"][:200], display_count=5)
            except httpx.HTTPError:
                continue
            want = _norm_title(row["title"])
            hit = next(
                (
                    a for a in articles
                    if a.art_id and _norm_title(a.title) == want
                    and (row["pubyear"] is None or a.pubyear is None or a.pubyear == row["pubyear"])
                ),
                None,
            )
            if hit is None:
                continue
            matched += 1
            print(f"  {row['external_id']} → {hit.art_id}  {row['title'][:40]}")
            if dry_run:
                continue
            async with AsyncSessionLocal() as session:
                exists = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM paper_citation_external_refs "
                            "WHERE source_cn = :s AND direction = :d AND external_id = :e"
                        ),
                        {"s": row["source_cn"], "d": row["direction"], "e": hit.art_id},
                    )
                ).scalar()
                if exists:
                    await session.execute(text("DELETE FROM paper_citation_external_refs WHERE id = :id"), {"id": row["id"]})
                else:
                    await session.execute(
                        text("UPDATE paper_citation_external_refs SET external_id = :e WHERE id = :id"),
                        {"e": hit.art_id, "id": row["id"]},
                    )
                await session.commit()
    print(f"[제목 매칭] {matched}건 KCI 논문으로 연결")
    return matched


def _corpus_cns() -> list[str]:
    data = json.loads(_PAPERS_PATH.read_text(encoding="utf-8"))
    return [p["CN"] for p in data["papers"] if p.get("CN")]


def _corpus_and_depth1_nodes(corpus: list[str]) -> list[str]:
    """KCI ID 코퍼스 논문 + 코퍼스가 인용한 1단계 국내 논문 노드 (Neo4j)."""
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            return [
                r["cn"]
                for r in session.run(
                    """
                    MATCH (p:Paper)
                    WHERE p.cn STARTS WITH 'ART'
                      AND (p.cn IN $corpus OR EXISTS { MATCH (c:Paper)-[:CITES]->(p) WHERE c.cn IN $corpus })
                    RETURN p.cn AS cn ORDER BY cn
                    """,
                    corpus=corpus,
                )
            ]
    finally:
        driver.close()


async def _graph_nodes_needing_refs(corpus: list[str]) -> list[str]:
    """그중 이 환경에 KCI 참고문헌을 아직 안 받은 것(papers 행 없음 또는 kci_refs_loaded_at NULL).
    Neo4j는 여러 환경이 같이 쓰므로 받았는지는 Postgres로 본다."""
    nodes = await asyncio.to_thread(_corpus_and_depth1_nodes, corpus)
    async with AsyncSessionLocal() as session:
        loaded = set(
            (
                await session.execute(
                    text("SELECT id FROM papers WHERE id = ANY(:ids) AND kci_refs_loaded_at IS NOT NULL"), {"ids": nodes}
                )
            ).scalars()
        )
    return [n for n in nodes if n not in loaded]


async def main() -> None:
    parser = argparse.ArgumentParser(description="1단계 국내(KCI) 참고문헌 논문 사전 적재")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--skip-title-match", action="store_true")
    args = parser.parse_args()

    if not args.skip_title_match:
        await match_korean_refs_by_title(args.dry_run)

    corpus = _corpus_cns()
    async with AsyncSessionLocal() as session:
        ref_targets = list((await session.execute(text(_ART_REFS_SQL), {"corpus": corpus})).scalars())
    graph_targets = await _graph_nodes_needing_refs(corpus)
    targets = list(dict.fromkeys(graph_targets + ref_targets))
    if args.limit:
        targets = targets[: args.limit]
    print(f"\n[적재] 참고문헌의 국내 논문 {len(ref_targets)}편 + 참고문헌 미적재 그래프 노드 {len(graph_targets)}편 "
          f"→ 대상 {len(targets)}편 (동시성 {args.concurrency})")
    if args.dry_run or not targets:
        return

    stats: Counter[str] = Counter()
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.time()
    done = 0

    async with httpx.AsyncClient(timeout=20.0) as http:
        async def one(art_id: str) -> None:
            nonlocal done
            async with semaphore:
                await asyncio.sleep(CALL_PACING_SECONDS)
                async with AsyncSessionLocal() as session:
                    try:
                        paper_id = await materialize_domestic_paper(session, art_id, client=http, retry_failed=True)
                        stats["적재" if paper_id else "KCI 없음"] += 1
                    except Exception as exc:
                        await session.rollback()
                        stats["오류"] += 1
                        print(f"  [오류] {art_id}: {exc!r}")
            done += 1
            if done % 100 == 0:
                rate = done / (time.time() - started)
                print(f"  {done}/{len(targets)} ({rate:.1f}편/초) {dict(stats)}")

        await asyncio.gather(*(one(t) for t in targets))

    print(f"\n[완료] {dict(stats)} / {(time.time() - started) / 60:.1f}분")


if __name__ == "__main__":
    asyncio.run(main())
