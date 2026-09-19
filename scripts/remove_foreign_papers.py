"""검색 코퍼스에서 해외 논문(db_code=JAFO)을 전 저장소에서 제거한다.

배경:
  코퍼스를 국내 논문 100%로 구성하기로 했다. JAFO 150건(전부 중국 의학 학술지)은 국내 논문과
  인용관계가 0건이라 인용관계 그래프의 해외 논문 노드로도 쓰이지 않는다 → 전량 삭제.
  판별은 반드시 db_code로 한다. id 접두사 NART는 국내 학술지(JAKO)에도 31건 있다.

지우는 것:
  Postgres  papers (CASCADE: paper_references / paper_similar / paper_citation_external_refs /
            researcher_papers / bookmarks / recent_reads), paper_selection_reasons,
            JAFO 논문으로만 들어온 연구자(researchers CASCADE + researcher_flow_cache)
  Neo4j     Paper 노드, 그 논문에만 붙어 있던 Keyword/Author/Journal/Year 노드,
            RELATED_TO paper_count 차감(0 이하면 삭제), 삭제 연구자의 ResearcherNode
  ChromaDB  papers / keywords(고아 키워드) / researchers
  파일      data/parsed/scienceon_keywords_normalized.json, abstract_sentence_embeddings.pkl

실행 전 scripts/backup_stores.py로 백업할 것. 기본은 dry-run(건수만 출력).

사용법:
  python scripts/remove_foreign_papers.py            # dry-run
  python scripts/remove_foreign_papers.py --apply
  python scripts/remove_foreign_papers.py --apply --skip-neo4j
      # Neo4j Aura는 로컬·운영이 같은 인스턴스다. 한 환경에서 이미 Neo4j를 정리했으면 다른 환경에서는
      # 반드시 이 옵션을 준다 — 안 주면 RELATED_TO paper_count가 두 번 차감된다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import sys
from collections import Counter
from itertools import combinations
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

import chromadb
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.neo4j_client import get_neo4j_driver
from app.core.settings import settings
from app.services.chroma_search_service import _PAPERS_PATH, _SENTENCE_CACHE_PATH
from scripts.load_neo4j_graph import _keyword_key, _string_list

FOREIGN_DB_CODE = "JAFO"


# ---------------------------------------------------------------------------
# 대상 산정
# ---------------------------------------------------------------------------

async def _load_targets() -> tuple[list[str], list[str]]:
    async with AsyncSessionLocal() as session:
        paper_ids = list(
            (await session.execute(text("SELECT id FROM papers WHERE db_code = :c ORDER BY id"), {"c": FOREIGN_DB_CODE})).scalars()
        )
        # 연결된 논문이 전부 해외 논문인 연구자 = 해외 논문 공저자 명단에서만 들어온 사람
        researcher_ids = list(
            (
                await session.execute(
                    text(
                        """
                        SELECT rp.researcher_id
                        FROM researcher_papers rp JOIN papers p ON p.id = rp.paper_id
                        GROUP BY rp.researcher_id
                        HAVING bool_and(p.db_code = :c)
                        """
                    ),
                    {"c": FOREIGN_DB_CODE},
                )
            ).scalars()
        )
    return paper_ids, researcher_ids


def _related_pairs(papers: list[dict]) -> Counter[tuple[str, str]]:
    """load_neo4j_graph.build_graph_payload와 같은 규칙으로 논문별 키워드 쌍을 센다
    (번역쌍 제외). 삭제할 논문이 RELATED_TO에 더해 놓은 몫을 되돌리는 데 쓴다."""
    counter: Counter[tuple[str, str]] = Counter()
    for paper in papers:
        ko = _string_list(paper.get("Keyword"))
        en = _string_list(paper.get("Keyword2"))
        translation_pairs: set[tuple[str, str]] = set()
        if len(ko) == len(en):
            for ko_kw, en_kw in zip(ko, en):
                a, b = _keyword_key(ko_kw, "ko"), _keyword_key(en_kw, "en")
                if a != b:
                    translation_pairs.add(tuple(sorted((a, b))))
        keys = sorted({_keyword_key(k, "ko") for k in ko} | {_keyword_key(k, "en") for k in en})
        for a, b in combinations(keys, 2):
            if (a, b) not in translation_pairs:
                counter[(a, b)] += 1
    return counter


# ---------------------------------------------------------------------------
# 저장소별 삭제
# ---------------------------------------------------------------------------

async def _delete_postgres(paper_ids: list[str], researcher_ids: list[str]) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM paper_selection_reasons WHERE paper_id = ANY(:ids)"), {"ids": paper_ids})
        await session.execute(text("DELETE FROM researcher_flow_cache WHERE researcher_id = ANY(:ids)"), {"ids": researcher_ids})
        r = await session.execute(text("DELETE FROM researchers WHERE researcher_id = ANY(:ids)"), {"ids": researcher_ids})
        print(f"[Postgres] researchers {r.rowcount}행 삭제")
        r = await session.execute(text("DELETE FROM papers WHERE id = ANY(:ids)"), {"ids": paper_ids})
        print(f"[Postgres] papers {r.rowcount}행 삭제 (연관 테이블은 CASCADE)")
        await session.commit()


def _delete_neo4j(paper_ids: list[str], researcher_ids: list[str], pairs: Counter[tuple[str, str]], *, apply: bool) -> list[str]:
    """삭제된 Keyword key 목록을 돌려준다 (Chroma keywords 정리용)."""
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            # 삭제 전에 이 논문들에 붙어 있던 주변 노드를 잡아 둔다 — 삭제 후 고아가 된 것만 지우기 위해
            # (원래부터 논문 없이 존재하던 노드는 건드리지 않는다)
            touched = session.run(
                """
                MATCH (p:Paper) WHERE p.cn IN $ids
                OPTIONAL MATCH (p)-[:HAS_KEYWORD]->(k:Keyword)
                OPTIONAL MATCH (a:Author)-[:AUTHORED]->(p)
                OPTIONAL MATCH (p)-[:PUBLISHED_IN]->(j:Journal)
                OPTIONAL MATCH (p)-[:PUBLISHED_IN_YEAR]->(y:Year)
                RETURN collect(DISTINCT k.key) AS keywords, collect(DISTINCT a.name) AS authors,
                       collect(DISTINCT j.name) AS journals, collect(DISTINCT y.value) AS years,
                       count(DISTINCT p) AS papers
                """,
                ids=paper_ids,
            ).single()
            orphan_keywords = session.run(
                """
                MATCH (k:Keyword) WHERE k.key IN $keys
                  AND NOT EXISTS { MATCH (p:Paper)-[:HAS_KEYWORD]->(k) WHERE NOT p.cn IN $ids }
                RETURN collect(k.key) AS keys
                """,
                keys=touched["keywords"], ids=paper_ids,
            ).single()["keys"]
            print(
                f"[Neo4j] Paper {touched['papers']} / 고아가 될 Keyword {len(orphan_keywords)} "
                f"(연결 {len(touched['keywords'])}) / RELATED_TO 차감 쌍 {len(pairs)} / ResearcherNode {len(researcher_ids)}"
            )
            if not apply:
                return orphan_keywords

            rows = [{"a": a, "b": b, "n": n} for (a, b), n in pairs.items()]
            for i in range(0, len(rows), 2000):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (a:Keyword {key: row.a})-[r:RELATED_TO]-(b:Keyword {key: row.b})
                    SET r.paper_count = coalesce(r.paper_count, 0) - row.n
                    WITH r WHERE r.paper_count <= 0
                    DELETE r
                    """,
                    rows=rows[i : i + 2000],
                ).consume()
            session.run("MATCH (p:Paper) WHERE p.cn IN $ids DETACH DELETE p", ids=paper_ids).consume()
            session.run(
                "MATCH (k:Keyword) WHERE k.key IN $keys AND NOT (k)<-[:HAS_KEYWORD]-(:Paper) DETACH DELETE k",
                keys=touched["keywords"],
            ).consume()
            session.run(
                "MATCH (a:Author) WHERE a.name IN $names AND NOT (a)-[:AUTHORED]->(:Paper) DETACH DELETE a",
                names=touched["authors"],
            ).consume()
            session.run(
                "MATCH (j:Journal) WHERE j.name IN $names AND NOT (:Paper)-[:PUBLISHED_IN]->(j) DETACH DELETE j",
                names=touched["journals"],
            ).consume()
            session.run(
                "MATCH (y:Year) WHERE y.value IN $values AND NOT (:Paper)-[:PUBLISHED_IN_YEAR]->(y) DETACH DELETE y",
                values=touched["years"],
            ).consume()
            session.run(
                "MATCH (r:ResearcherNode) WHERE r.researcher_id IN $ids DETACH DELETE r", ids=researcher_ids
            ).consume()
            print("[Neo4j] 삭제 완료")
            return orphan_keywords
    finally:
        driver.close()


def _keywords_missing_from_neo4j() -> list[str]:
    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    chroma_keys = set(client.get_collection("keywords").get(include=[])["ids"])
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            neo4j_keys = {r["key"] for r in session.run("MATCH (k:Keyword) RETURN k.key AS key")}
    finally:
        driver.close()
    return sorted(chroma_keys - neo4j_keys)


def _delete_chroma(paper_ids: list[str], keyword_keys: list[str], researcher_ids: list[str]) -> None:
    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    for name, ids in (("papers", paper_ids), ("keywords", keyword_keys), ("researchers", researcher_ids)):
        collection = client.get_collection(name)
        before = collection.count()
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            if batch:
                collection.delete(ids=batch)
        print(f"[Chroma] {name}: {before} → {collection.count()}")


def _rewrite_corpus_files(paper_ids: set[str]) -> None:
    data = json.loads(_PAPERS_PATH.read_text(encoding="utf-8"))
    before = len(data["papers"])
    data["papers"] = [p for p in data["papers"] if p.get("CN") not in paper_ids]
    data.setdefault("meta", {})["total_count"] = len(data["papers"])
    _PAPERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[파일] {_PAPERS_PATH.name}: {before} → {len(data['papers'])}")

    with open(_SENTENCE_CACHE_PATH, "rb") as f:
        cache = pickle.load(f)
    before = len(cache)
    cache = {k: v for k, v in cache.items() if k not in paper_ids}
    with open(_SENTENCE_CACHE_PATH, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"[파일] {_SENTENCE_CACHE_PATH.name}: {before} → {len(cache)}")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="해외 논문(JAFO) 전 저장소 제거")
    parser.add_argument("--apply", action="store_true", help="실제로 삭제한다 (없으면 dry-run)")
    parser.add_argument("--skip-neo4j", action="store_true", help="Neo4j는 건드리지 않는다 (공유 Aura를 이미 정리한 경우)")
    args = parser.parse_args()

    paper_ids, researcher_ids = await _load_targets()
    corpus = json.loads(_PAPERS_PATH.read_text(encoding="utf-8"))["papers"]
    target_set = set(paper_ids)
    foreign_in_corpus = [p for p in corpus if p.get("CN") in target_set or p.get("DBCode") == FOREIGN_DB_CODE]
    # JSON에만 남아 있는 해외 논문도 같이 지운다
    target_set |= {p["CN"] for p in foreign_in_corpus}
    paper_ids = sorted(target_set)
    pairs = _related_pairs(foreign_in_corpus)

    print(f"=== 대상: 해외 논문 {len(paper_ids)}건 / 연구자 {len(researcher_ids)}명 ({'APPLY' if args.apply else 'dry-run'}) ===")
    if not paper_ids:
        print("삭제할 해외 논문이 없습니다.")
        return

    if args.skip_neo4j:
        # Neo4j에서 이미 지워져 고아 키워드를 다시 셀 수 없다 — Chroma keywords는 Neo4j에 없는 key로 정리한다
        orphan_keywords = _keywords_missing_from_neo4j()
        print(f"[Neo4j] 건너뜀 — Chroma keywords 중 Neo4j에 없는 key {len(orphan_keywords)}개를 정리 대상으로 잡음")
    else:
        orphan_keywords = _delete_neo4j(paper_ids, researcher_ids, pairs, apply=args.apply)
    if not args.apply:
        print("[dry-run] 쓰기 없음. --apply로 실행하세요.")
        return

    await _delete_postgres(paper_ids, researcher_ids)
    _delete_chroma(paper_ids, orphan_keywords, researcher_ids)
    _rewrite_corpus_files(target_set)
    print("[완료] 서버 재시작 필요 (검색 서비스가 코퍼스 JSON/문장 캐시를 기동 시 메모리에 올림)")


if __name__ == "__main__":
    asyncio.run(main())
