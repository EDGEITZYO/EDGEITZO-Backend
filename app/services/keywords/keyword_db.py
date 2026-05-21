from dataclasses import dataclass
from typing import Optional

from app.core.neo4j_client import get_neo4j_driver


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


def search_keywords(query: str, lang: str | None = None, limit: int = 5) -> list[Keyword]:
    """Neo4j fulltext 인덱스(keyword_name_fulltext)로 키워드 검색"""
    # 공백 포함 시 phrase 검색으로 처리
    ft_query = f'"{query}"' if " " in query else query

    cypher = """
    CALL db.index.fulltext.queryNodes("keyword_name_fulltext", $ft_query)
    YIELD node AS k, score
    WHERE ($lang IS NULL OR k.lang = $lang)
    OPTIONAL MATCH (p:Paper)-[:HAS_KEYWORD]->(k)
    RETURN k, score, count(DISTINCT p) AS paper_count
    ORDER BY score DESC, paper_count DESC
    LIMIT $limit
    """

    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            records = list(session.run(cypher, ft_query=ft_query, lang=lang, limit=limit))
    finally:
        driver.close()

    return [_to_keyword(dict(r["k"]), r["paper_count"]) for r in records]


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
