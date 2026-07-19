from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_CYPHER_IDS_BY_KEYWORD = """
MATCH (p:Paper)-[:HAS_KEYWORD]->(k:Keyword)
WHERE k.key = $keyword OR k.name = $keyword OR k.normalized_name = $normalized
RETURN DISTINCT p.cn AS cn
LIMIT $limit
"""


async def get_paper_ids_by_keyword(keyword: str, limit: int = 200) -> list[str]:
    """키워드 이름 또는 key(예: ko:생명공학)로 Neo4j에서 Paper CN 목록 조회.
    인스턴스가 꺼져있으면 빈 리스트 반환 — 호출부에서 fallback 처리.
    """
    try:
        from app.core.neo4j_client import get_neo4j_driver

        normalized = keyword.strip().casefold()

        def _query() -> list[str]:
            driver = get_neo4j_driver()
            try:
                with driver.session() as session:
                    records = session.run(
                        _CYPHER_IDS_BY_KEYWORD,
                        keyword=keyword,
                        normalized=normalized,
                        limit=limit,
                    )
                    return [r["cn"] for r in records if r["cn"]]
            finally:
                driver.close()

        return await asyncio.to_thread(_query)
    except Exception as e:
        logger.warning("Neo4j 논문 ID 조회 실패, 빈 리스트 반환: %s", e)
        return []
