from typing import Any, Optional


class GraphRepository:
    def __init__(self, driver: Any):
        self.driver = driver

    @staticmethod
    def _keyword_node_to_dict(
        keyword: Any,
        *,
        paper_count: int = 0,
        is_center: bool = False,
    ) -> dict[str, Any]:
        data = dict(keyword)
        return {
            "key": data.get("key"),
            "name": data.get("name"),
            "normalized_name": data.get("normalized_name"),
            "lang": data.get("lang"),
            "source_field": data.get("source_field"),
            "paper_count": paper_count,
            "is_center": is_center,
        }

    @staticmethod
    def _relationship_to_edge(
        relationship: Any,
        *,
        center_key: str,
        related_key: str,
    ) -> dict[str, Any]:
        data = dict(relationship)
        return {
            "source": center_key,
            "target": related_key,
            "paper_count": data.get("paper_count", 0),
            "lang_pair": data.get("lang_pair"),
        }

    def find_keyword(
        self,
        keyword: str,
        *,
        lang: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        normalized_keyword = keyword.casefold()

        query = """
        MATCH (k:Keyword)
        WHERE (
            k.key = $keyword
            OR k.normalized_name = $normalized_keyword
            OR toLower(k.name) = $normalized_keyword
        )
        AND ($lang IS NULL OR k.lang = $lang)
        OPTIONAL MATCH (p:Paper)-[:HAS_KEYWORD]->(k)
        RETURN k AS keyword, count(DISTINCT p) AS paper_count
        ORDER BY
            CASE WHEN k.key = $keyword THEN 0 ELSE 1 END,
            paper_count DESC,
            k.name ASC
        LIMIT 1
        """

        with self.driver.session() as session:
            record = session.run(
                query,
                keyword=keyword,
                normalized_keyword=normalized_keyword,
                lang=lang,
            ).single()

        if record is None:
            return None

        return self._keyword_node_to_dict(
            record["keyword"],
            paper_count=record["paper_count"],
            is_center=True,
        )

    def find_related_keywords(
        self,
        keyword_key: str,
        *,
        limit: int,
        min_paper_count: int,
    ) -> list[dict[str, Any]]:
        query = """
        MATCH (:Keyword {key: $keyword_key})-[r:RELATED_TO]-(related:Keyword)
        WHERE r.paper_count >= $min_paper_count
        OPTIONAL MATCH (related)<-[:HAS_KEYWORD]-(p:Paper)
        RETURN related AS keyword,
               r AS relationship,
               count(DISTINCT p) AS paper_count
        ORDER BY r.paper_count DESC, related.name ASC
        LIMIT $limit
        """

        with self.driver.session() as session:
            records = list(
                session.run(
                    query,
                    keyword_key=keyword_key,
                    limit=limit,
                    min_paper_count=min_paper_count,
                )
            )

        related_items: list[dict[str, Any]] = []
        for record in records:
            related_node = self._keyword_node_to_dict(
                record["keyword"],
                paper_count=record["paper_count"],
                is_center=False,
            )
            related_items.append(
                {
                    "node": related_node,
                    "edge": self._relationship_to_edge(
                        record["relationship"],
                        center_key=keyword_key,
                        related_key=related_node["key"],
                    ),
                }
            )

        return related_items
