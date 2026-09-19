"""검색 코퍼스에 국내(KCI) 논문을 추가해 목표 건수(기본 1,000편)를 채운다.

해외 논문(JAFO) 150편을 뺀 자리를 채우기 위한 스크립트다(scripts/remove_foreign_papers.py 다음 단계).
후보는 scripts/load_domestic_reference_papers.py로 이미 papers·Neo4j에 적재된 국내 논문 중
**현재 코퍼스 논문이 인용한 것**이다. 코퍼스가 실제로 기대는 선행 연구라 주제가 맞고,
검색 코퍼스에 넣으면 인용관계 그래프의 1단계 노드도 그대로 코퍼스 논문이 된다.

선정 기준 (순서대로):
  - 한글 초록 200자 이상, 한글 키워드 3개 이상, 2015년 이후 (코퍼스 JAKO는 2018~2025년이 대부분)
  - 주제 점수 내림차순: 원래 코퍼스 수집 키워드(scripts/collect_papers.py KEYWORDS 11개)와의
    BGE 임베딩 코사인 유사도 최댓값. 코퍼스 키워드와 글자만 겹치는지 보는 방식은 "유전자 알고리즘
    기반 주가 예측" 같은 논문이 통과해서 쓰지 않는다. (2026-09-19 실측: 기존 코퍼스 중앙값 0.36,
    선정 150편 하한 0.353)
  - "유전자 알고리즘"은 수집 키워드 "유전자"와 표기만 같은 최적화 기법이라 제외

이 스크립트가 하는 것:
  - data/parsed/scienceon_keywords_normalized.json 에 레코드 추가
  - papers.source = 'knowledge_base' (코퍼스 표식)
  - Neo4j 키워드 층: HAS_KEYWORD / AUTHORED / PUBLISHED_IN / PUBLISHED_IN_YEAR, RELATED_TO paper_count 증가

이어서 돌릴 것 (임베딩):
  python scripts/embed_papers.py --skip-existing
  python scripts/embed_abstract_sentences.py --only-missing
  python scripts/embed_keywords.py --skip-existing
그 뒤 서버 재시작 (검색 서비스가 코퍼스 JSON·문장 캐시를 기동 시 메모리에 올림).

사용법:
  python scripts/add_domestic_corpus_papers.py --dry-run
  python scripts/add_domestic_corpus_papers.py            # 1,000편이 될 때까지
  python scripts/add_domestic_corpus_papers.py --target 1000
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

import re

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.neo4j_client import get_neo4j_driver
from app.services.chroma_search_service import _PAPERS_PATH
from scripts.collect_papers import KEYWORDS as COLLECTION_KEYWORDS
from scripts.embed_papers import MODEL_NAME, _build_text
from scripts.load_neo4j_graph import GraphPayload, build_graph_payload, load_payload
from scripts.remove_foreign_papers import _related_pairs

CORPUS_SOURCE = "knowledge_base"
MIN_PUBYEAR = 2015
MIN_ABSTRACT_CHARS = 200
MIN_KEYWORDS = 3

_CANDIDATES_SQL = """
SELECT id, title, title_en, abstract, abstract_en, authors, keywords_ko, keywords_en,
       issn, doi, pubyear, pubdate, journal_name
FROM papers
WHERE id = ANY(:ids)
  AND db_code = 'JAKO'
  AND abstract ~ '[가-힣]' AND length(abstract) >= :min_abs
  AND cardinality(keywords_ko) >= :min_kw
  AND pubyear >= :min_year
"""


def _cited_by_corpus(corpus_cns: list[str]) -> Counter[str]:
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            records = session.run(
                """
                MATCH (a:Paper)-[:CITES]->(b:Paper)
                WHERE a.cn IN $corpus AND NOT b.cn IN $corpus
                RETURN b.cn AS cn, count(DISTINCT a) AS n
                """,
                corpus=corpus_cns,
            )
            return Counter({r["cn"]: r["n"] for r in records})
    finally:
        driver.close()


_GENETIC_ALGORITHM_RE = re.compile(r"유전자\s*알고리즘|genetic\s+algorithm", re.IGNORECASE)


def _topic_scores(rows: list[dict]) -> dict[str, float]:
    """수집 키워드 11개와의 코사인 유사도 최댓값 (문서는 embed_papers와 같은 텍스트, 쿼리는 'query: ' prefix)."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME, device="cpu")
    docs = model.encode(
        [_build_text({"Title": r["title"], "Abstract": r["abstract"], "Keyword": r["keywords_ko"]}) for r in rows],
        normalize_embeddings=True,
        batch_size=8,
    )
    queries = model.encode([f"query: {k}" for k in COLLECTION_KEYWORDS], normalize_embeddings=True)
    return {r["id"]: float(score) for r, score in zip(rows, (docs @ queries.T).max(axis=1))}


def _to_record(row: dict) -> dict:
    keywords_ko = list(row["keywords_ko"] or [])
    keywords_en = list(row["keywords_en"] or [])
    return {
        "CN": row["id"],
        "DBCode": "JAKO",
        "Title": row["title"],
        "Title2": row["title_en"],
        "Abstract": row["abstract"],
        "Abstract2": row["abstract_en"],
        "Keyword": keywords_ko,
        "keyword_raw": " . ".join(keywords_ko) or None,
        "Keyword2": keywords_en,
        "keyword2_raw": " . ".join(keywords_en) or None,
        "ISSN": [row["issn"]] if row["issn"] else None,
        "DOI": row["doi"],
        "Pubyear": row["pubyear"],
        "Pubdate": (row["pubdate"] or "").replace("-", ".") or None,
        "JournalName": row["journal_name"],
        "Author": list(row["authors"] or []),
        "references": [],
        "similar": [],
    }


def _load_neo4j(records: list[dict]) -> None:
    payload = build_graph_payload({"papers": records})
    # RELATED_TO는 load_payload가 paper_count를 SET(덮어쓰기)하므로 빼고, 아래에서 기존 값에 더한다
    without_related = GraphPayload(**{**payload.__dict__, "related_keywords": []})
    pairs = _related_pairs(records)
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            load_payload(session, without_related, batch_size=500)
            # _related_pairs의 쌍은 key 정렬 순(a < b) — load_neo4j_graph가 만든 엣지 방향과 같다
            rows = [
                {"a": a, "b": b, "n": n, "lang_pair": "-".join(sorted((a.split(":", 1)[0], b.split(":", 1)[0])))}
                for (a, b), n in pairs.items()
            ]
            for i in range(0, len(rows), 2000):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (a:Keyword {key: row.a})
                    MATCH (b:Keyword {key: row.b})
                    MERGE (a)-[r:RELATED_TO]->(b)
                    ON CREATE SET r.paper_count = row.n, r.lang_pair = row.lang_pair
                    ON MATCH SET r.paper_count = coalesce(r.paper_count, 0) + row.n
                    """,
                    rows=rows[i : i + 2000],
                ).consume()
            print(f"[Neo4j] RELATED_TO 증가 {len(rows)}쌍")
    finally:
        driver.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="검색 코퍼스를 국내 논문으로 채운다")
    parser.add_argument("--target", type=int, default=1000, help="코퍼스 목표 건수 (기본 1000)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = json.loads(_PAPERS_PATH.read_text(encoding="utf-8"))
    corpus = data["papers"]
    need = args.target - len(corpus)
    print(f"현재 코퍼스 {len(corpus)}편 → 목표 {args.target}편, 추가 {max(need, 0)}편")
    if need <= 0:
        return

    corpus_cns = [p["CN"] for p in corpus]
    cited = await asyncio.to_thread(_cited_by_corpus, corpus_cns)
    async with AsyncSessionLocal() as session:
        rows = [
            dict(r)
            for r in (
                await session.execute(
                    text(_CANDIDATES_SQL),
                    {"ids": list(cited), "min_abs": MIN_ABSTRACT_CHARS, "min_kw": MIN_KEYWORDS, "min_year": MIN_PUBYEAR},
                )
            ).mappings()
        ]
    rows = [r for r in rows if not _GENETIC_ALGORITHM_RE.search(f"{r['title']} {' '.join(r['keywords_ko'])}")]
    scores = _topic_scores(rows)
    rows.sort(key=lambda r: (scores[r["id"]], cited[r["id"]]), reverse=True)
    chosen = rows[:need]
    print(
        f"코퍼스가 인용한 국내 논문 {len(cited)}편 → 초록·키워드·연도 통과 {len(rows)}편 → 주제 점수 상위 {len(chosen)}편 "
        f"(점수 {scores[chosen[-1]['id']]:.3f}~{scores[chosen[0]['id']]:.3f})"
        if chosen else "선정 가능한 후보가 없습니다"
    )
    print("  인용한 코퍼스 논문 수 분포:", dict(Counter(cited[r["id"]] for r in chosen)))
    print("  연도 분포:", dict(sorted(Counter(r["pubyear"] for r in chosen).items())))
    for r in chosen[:5] + chosen[-5:]:
        print(f"  {scores[r['id']]:.3f} {r['id']} ({r['pubyear']}, 인용 {cited[r['id']]}) {r['title'][:50]}")
    if len(chosen) < need:
        print(f"[주의] 후보가 {need - len(chosen)}편 모자랍니다")
    if args.dry_run or not chosen:
        return

    records = [_to_record(r) for r in chosen]
    await asyncio.to_thread(_load_neo4j, records)

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE papers SET source = :s, updated_at = now() WHERE id = ANY(:ids)"),
            {"s": CORPUS_SOURCE, "ids": [r["id"] for r in chosen]},
        )
        await session.commit()
    print(f"[Postgres] source='{CORPUS_SOURCE}' {len(chosen)}편")

    data["papers"] = corpus + records
    data.setdefault("meta", {})["total_count"] = len(data["papers"])
    _PAPERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[파일] {_PAPERS_PATH.name}: {len(corpus)} → {len(data['papers'])}")
    print("\n다음: embed_papers.py --skip-existing / embed_abstract_sentences.py --only-missing / "
          "embed_keywords.py --skip-existing → 서버 재시작")


if __name__ == "__main__":
    asyncio.run(main())
