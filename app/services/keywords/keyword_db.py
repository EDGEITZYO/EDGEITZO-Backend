from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.neo4j_client import get_neo4j_driver
from app.services.keywords.keyword_embedding_search import embedding_search

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MISSED_QUERIES_LOG = _PROJECT_ROOT / "data" / "keyword_search_misses.log"

# Lucene 예약문자. 사용자는 검색 문법을 쓸 의도가 없으므로 공백으로 치환해
# 쿼리 문법으로 오인되는 것을 막는다 (예: "유전체(genome)" 의 괄호).
_LUCENE_RESERVED_RE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')

# 완전 일치/AND 매칭으로 못 찾는 동의어·이표기 쌍.
# 아래 실패 로그(_MISSED_QUERIES_LOG)를 보고 계속 채워 넣는다.
_SYNONYMS: dict[str, list[str]] = {
    "게놈": ["유전체"],
    "유전체": ["게놈"],
}


@dataclass
class Keyword:
    key: str
    name_ko: Optional[str]
    name_en: Optional[str]
    paper_count: int


def _to_keyword(node_data: dict, paper_count: int) -> Keyword:
    """Neo4j Keyword 노드 → Keyword dataclass"""
    lang = node_data.get("lang")
    name = node_data.get("name", "")
    return Keyword(
        key=node_data.get("key", ""),
        name_ko=name if lang == "ko" else None,
        name_en=name if lang == "en" else None,
        paper_count=paper_count,
    )


_SEARCH_CYPHER = """
CALL db.index.fulltext.queryNodes("keyword_name_fulltext", $ft_query)
YIELD node AS k, score
WHERE ($lang IS NULL OR k.lang = $lang)
OPTIONAL MATCH (p:Paper)-[:HAS_KEYWORD]->(k)
RETURN k, score, count(DISTINCT p) AS paper_count
ORDER BY score DESC, paper_count DESC
LIMIT $limit
"""


def _run_fulltext_query(ft_query: str, lang: str | None, limit: int) -> list:
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            return list(session.run(_SEARCH_CYPHER, ft_query=ft_query, lang=lang, limit=limit))
    finally:
        driver.close()


def _sanitize_query(query: str) -> str:
    return _LUCENE_RESERVED_RE.sub(" ", query).strip()


def _to_ft_query(text: str) -> str:
    return f'"{text}"' if " " in text else text


def _record_miss(query: str) -> None:
    """동의어 사전으로도 못 찾은 검색어 기록 (추후 _SYNONYMS 보강용)"""
    logger.warning("키워드 검색 실패(동의어 사전 보강 후보): %s", query)
    try:
        _MISSED_QUERIES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _MISSED_QUERIES_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{query}\n")
    except OSError:
        logger.warning("키워드 검색 실패 로그 기록 실패", exc_info=True)


def search_keywords(query: str, lang: str | None = None, limit: int = 5) -> list[Keyword]:
    """Neo4j fulltext 인덱스(keyword_name_fulltext)로 키워드 검색

    1. Lucene 예약문자(괄호, 콜론 등)를 공백으로 치환해 쿼리 문법으로 오인되지 않게 함
    2. 여러 단어면 먼저 phrase(정확한 연속 어구) 매칭을 시도
    3. 실패하면 입력 단어를 모두 포함하되 순서는 무관한(AND) 조건으로 완화해 재시도
    4. 그래도 실패하면 동의어 사전(_SYNONYMS)에서 대체어로 재시도
    5. 그래도 실패하면 임베딩(BGE-m3-ko) 기반 의미 검색으로 재시도 (임계값 미만은 결과 없음 처리)
    6. 전부 실패하면 추후 동의어 사전 보강을 위해 검색어를 로그로 남김
    """
    sanitized = _sanitize_query(query)
    if not sanitized:
        return []

    tokens = sanitized.split()
    records = _run_fulltext_query(_to_ft_query(sanitized), lang, limit)

    if not records and len(tokens) > 1:
        and_query = " ".join(f"+{token}" for token in tokens)
        records = _run_fulltext_query(and_query, lang, limit)

    if not records:
        for synonym in _SYNONYMS.get(sanitized, []):
            records = _run_fulltext_query(_to_ft_query(synonym), lang, limit)
            if records:
                break

    if records:
        return [_to_keyword(dict(r["k"]), r["paper_count"]) for r in records]

    matches = embedding_search(sanitized, lang=lang, limit=limit)
    if matches:
        return [_to_keyword(m, m.get("paper_count", 0)) for m in matches]

    _record_miss(query)
    return []


def get_keyword(key: str) -> Keyword | None:
    """key로 단일 키워드 조회"""
    cypher = """
    MATCH (k:Keyword {key: $key})
    OPTIONAL MATCH (p:Paper)-[:HAS_KEYWORD]->(k)
    RETURN k, count(DISTINCT p) AS paper_count
    """

    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            record = session.run(cypher, key=key).single()
    finally:
        driver.close()

    if record is None:
        return None

    return _to_keyword(dict(record["k"]), record["paper_count"])
