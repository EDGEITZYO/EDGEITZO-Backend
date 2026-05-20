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

    @staticmethod
    def _paper_to_dict(
        paper: Any,
        *,
        authors: list[str] | None = None,
        keywords: list[str] | None = None,
        keyword_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        data = dict(paper)
        return {
            "cn": data.get("cn"),
            "db_code": data.get("db_code"),
            "title": data.get("title"),
            "title_en": data.get("title_en"),
            "abstract": data.get("abstract"),
            "abstract_en": data.get("abstract_en"),
            "doi": data.get("doi"),
            "pubyear": data.get("pubyear"),
            "journal_name": data.get("journal_name"),
            "authors": [name for name in authors or [] if name],
            "keywords": [name for name in keywords or [] if name],
            "keyword_keys": [key for key in keyword_keys or [] if key],
        }

    @staticmethod
    def _paper_graph_node(paper: Any, *, is_center: bool = False) -> dict[str, Any]:
        data = dict(paper)
        cn = data.get("cn")
        return {
            "id": f"paper:{cn}",
            "label": "Paper",
            "name": data.get("title") or data.get("title_en") or cn,
            "is_center": is_center,
            "properties": {
                "cn": cn,
                "db_code": data.get("db_code"),
                "title": data.get("title"),
                "title_en": data.get("title_en"),
                "abstract": data.get("abstract"),
                "abstract_en": data.get("abstract_en"),
                "doi": data.get("doi"),
                "pubyear": data.get("pubyear"),
                "journal_name": data.get("journal_name"),
            },
        }

    @staticmethod
    def _keyword_graph_node(keyword: Any) -> dict[str, Any]:
        data = dict(keyword)
        key = data.get("key")
        return {
            "id": f"keyword:{key}",
            "label": "Keyword",
            "name": data.get("name") or key,
            "is_center": False,
            "properties": {
                "key": key,
                "normalized_name": data.get("normalized_name"),
                "lang": data.get("lang"),
                "source_field": data.get("source_field"),
            },
        }

    @staticmethod
    def _author_graph_node(author: Any) -> dict[str, Any]:
        data = dict(author)
        name = data.get("name")
        return {
            "id": f"author:{name}",
            "label": "Author",
            "name": name,
            "is_center": False,
            "properties": {"name": name},
        }

    @staticmethod
    def _journal_graph_node(journal: Any) -> dict[str, Any]:
        data = dict(journal)
        name = data.get("name")
        return {
            "id": f"journal:{name}",
            "label": "Journal",
            "name": name,
            "is_center": False,
            "properties": {"name": name},
        }

    @staticmethod
    def _year_graph_node(year: Any) -> dict[str, Any]:
        data = dict(year)
        value = data.get("value")
        return {
            "id": f"year:{value}",
            "label": "Year",
            "name": str(value),
            "is_center": False,
            "properties": {"value": value},
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

    def find_papers_by_keyword(
        self,
        keyword_key: str,
        *,
        limit: int,
        offset: int,
    ) -> Optional[dict[str, Any]]:
        keyword_query = """
        MATCH (k:Keyword {key: $keyword_key})
        OPTIONAL MATCH (p:Paper)-[:HAS_KEYWORD]->(k)
        RETURN k AS keyword, count(DISTINCT p) AS total_count
        """

        papers_query = """
        MATCH (:Keyword {key: $keyword_key})<-[:HAS_KEYWORD]-(p:Paper)
        OPTIONAL MATCH (a:Author)-[authored:AUTHORED]->(p)
        WITH p, a, authored
        ORDER BY authored.order ASC
        WITH p, collect(a.name) AS authors
        OPTIONAL MATCH (p)-[:HAS_KEYWORD]->(keyword:Keyword)
        WITH p, authors, keyword
        ORDER BY keyword.lang ASC, keyword.name ASC
        WITH p,
             authors,
             collect(keyword.name) AS keywords,
             collect(keyword.key) AS keyword_keys
        RETURN p AS paper,
               authors,
               keywords,
               keyword_keys
        ORDER BY coalesce(p.pubyear, 0) DESC, p.title ASC
        SKIP $offset
        LIMIT $limit
        """

        with self.driver.session() as session:
            keyword_record = session.run(keyword_query, keyword_key=keyword_key).single()
            if keyword_record is None:
                return None

            paper_records = list(
                session.run(
                    papers_query,
                    keyword_key=keyword_key,
                    offset=offset,
                    limit=limit,
                )
            )

        keyword = self._keyword_node_to_dict(
            keyword_record["keyword"],
            paper_count=keyword_record["total_count"],
            is_center=True,
        )
        papers = [
            self._paper_to_dict(
                record["paper"],
                authors=record["authors"],
                keywords=record["keywords"],
                keyword_keys=record["keyword_keys"],
            )
            for record in paper_records
        ]

        return {
            "keyword": keyword,
            "total_count": keyword_record["total_count"],
            "papers": papers,
        }

    def find_paper_neighbor_graph(
        self,
        paper_cn: str,
        *,
        keyword_limit: int,
    ) -> Optional[dict[str, Any]]:
        paper_query = "MATCH (p:Paper {cn: $paper_cn}) RETURN p AS paper"
        keyword_query = """
        MATCH (p:Paper {cn: $paper_cn})-[r:HAS_KEYWORD]->(k:Keyword)
        RETURN k AS keyword, r AS relationship
        ORDER BY k.lang ASC, k.name ASC
        LIMIT $keyword_limit
        """
        author_query = """
        MATCH (a:Author)-[r:AUTHORED]->(p:Paper {cn: $paper_cn})
        RETURN a AS author, r AS relationship
        ORDER BY r.order ASC, a.name ASC
        """
        journal_query = """
        MATCH (p:Paper {cn: $paper_cn})-[r:PUBLISHED_IN]->(j:Journal)
        RETURN j AS journal, r AS relationship
        """
        year_query = """
        MATCH (p:Paper {cn: $paper_cn})-[r:PUBLISHED_IN_YEAR]->(y:Year)
        RETURN y AS year, r AS relationship
        """

        with self.driver.session() as session:
            paper_record = session.run(paper_query, paper_cn=paper_cn).single()
            if paper_record is None:
                return None

            keyword_records = list(
                session.run(
                    keyword_query,
                    paper_cn=paper_cn,
                    keyword_limit=keyword_limit,
                )
            )
            author_records = list(session.run(author_query, paper_cn=paper_cn))
            journal_records = list(session.run(journal_query, paper_cn=paper_cn))
            year_records = list(session.run(year_query, paper_cn=paper_cn))

        center = self._paper_graph_node(paper_record["paper"], is_center=True)
        nodes: list[dict[str, Any]] = [center]
        edges: list[dict[str, Any]] = []

        for record in keyword_records:
            keyword_node = self._keyword_graph_node(record["keyword"])
            nodes.append(keyword_node)
            relationship = dict(record["relationship"])
            edges.append(
                {
                    "source": center["id"],
                    "target": keyword_node["id"],
                    "type": "HAS_KEYWORD",
                    "properties": {
                        "lang": relationship.get("lang"),
                        "source_field": relationship.get("source_field"),
                    },
                }
            )

        for record in author_records:
            author_node = self._author_graph_node(record["author"])
            nodes.append(author_node)
            relationship = dict(record["relationship"])
            edges.append(
                {
                    "source": author_node["id"],
                    "target": center["id"],
                    "type": "AUTHORED",
                    "properties": {"order": relationship.get("order")},
                }
            )

        for record in journal_records:
            journal_node = self._journal_graph_node(record["journal"])
            nodes.append(journal_node)
            edges.append(
                {
                    "source": center["id"],
                    "target": journal_node["id"],
                    "type": "PUBLISHED_IN",
                    "properties": {},
                }
            )

        for record in year_records:
            year_node = self._year_graph_node(record["year"])
            nodes.append(year_node)
            edges.append(
                {
                    "source": center["id"],
                    "target": year_node["id"],
                    "type": "PUBLISHED_IN_YEAR",
                    "properties": {},
                }
            )

        unique_nodes = {node["id"]: node for node in nodes}
        return {
            "center": center,
            "nodes": list(unique_nodes.values()),
            "edges": edges,
        }
