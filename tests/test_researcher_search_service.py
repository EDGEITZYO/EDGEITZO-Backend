import json

import pytest

from app.schemas.researcher import ResearcherSearchItem
from app.services import researcher_search_service as service


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, int | None]] = []

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        self.set_calls.append((key, ex))


class FakeQueryResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeSearchDb:
    def __init__(self, *, name_count: int, rows: list[dict]):
        self.name_count = name_count
        self.rows = rows
        self.scalar_calls: list[tuple[object, dict]] = []
        self.execute_calls: list[tuple[object, dict]] = []

    async def scalar(self, sql, params):
        self.scalar_calls.append((sql, params))
        return self.name_count

    async def execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return FakeQueryResult(self.rows)


def _researcher(
    researcher_id: str,
    *,
    name: str,
    keywords: list[str],
    total_citations: int,
    citation_source: str,
    field_paper_count: int,
    relevance_score: float,
) -> ResearcherSearchItem:
    return ResearcherSearchItem(
        researcher_id=researcher_id,
        source="kci",
        author_name_kor=name,
        institution_current="Test University",
        institution_dept="Test Department",
        keywords=keywords,
        total_papers=10,
        total_citations=total_citations,
        citation_source=citation_source,
        corpus_paper_count=3,
        field_paper_count=field_paper_count,
        matched_keywords=keywords[:1],
        relevance_score=relevance_score,
    )


def _field_row(
    researcher_id: str,
    *,
    relevance_score: float,
    field_paper_count: int = 1,
    total_citations: int = 0,
    total_count: int = 2,
) -> dict:
    return {
        "researcher_id": researcher_id,
        "source": "kci",
        "scienceon_cn": None,
        "author_name_kor": researcher_id,
        "author_name_eng": None,
        "institution_current": "Test University",
        "institution_dept": "Test Department",
        "keywords": ["biology"],
        "total_papers": 10,
        "total_citations": total_citations,
        "citation_source": "kci",
        "corpus_paper_count": 3,
        "first_pubyear": 2020,
        "last_pubyear": 2024,
        "field_paper_count": field_paper_count,
        "keyword_match_count": 1,
        "researcher_matched_keywords": ["biology"],
        "internal_matched_keywords": [],
        "external_matched_keywords": [],
        "relevance_score": relevance_score,
        "total_count": total_count,
    }


def test_build_researcher_graph_returns_only_field_to_researcher_edges():
    items = [
        _researcher(
            "kci:one",
            name="Researcher One",
            keywords=["biology", "shared"],
            total_citations=12,
            citation_source="kci",
            field_paper_count=2,
            relevance_score=0.8,
        ),
        _researcher(
            "oa:two",
            name="Researcher Two",
            keywords=["chemistry", "shared"],
            total_citations=34,
            citation_source="openalex",
            field_paper_count=1,
            relevance_score=0.4,
        ),
    ]

    graph = service.build_researcher_graph("biology", items)

    assert graph.query == "biology"
    assert len(graph.nodes) == 3
    assert len(graph.edges) == 2
    assert {edge.edge_type for edge in graph.edges} == {"field_relevance"}
    assert {edge.source for edge in graph.edges} == {"field:biology"}
    assert {edge.target for edge in graph.edges} == {"researcher:kci:one", "researcher:oa:two"}
    assert [edge.weight for edge in graph.edges] == [0.8, 0.4]

    researcher_nodes = [node for node in graph.nodes if node.node_type == "researcher"]
    assert researcher_nodes[0].citation_source == "kci"
    assert researcher_nodes[1].citation_source == "openalex"


@pytest.mark.asyncio
async def test_field_search_executes_sql_and_ranks_by_embedding_affinity(monkeypatch):
    db = FakeSearchDb(
        name_count=0,
        rows=[
            _field_row("low-affinity", relevance_score=100.0, field_paper_count=5, total_citations=90),
            _field_row("high-affinity", relevance_score=1.0, field_paper_count=1, total_citations=0),
        ],
    )

    async def fake_field_affinity(db_arg, keyword, researcher_ids):
        assert db_arg is db
        assert keyword == "biology"
        assert researcher_ids == ["low-affinity", "high-affinity"]
        return [
            {"researcher_id": "low-affinity", "affinity": 0.2},
            {"researcher_id": "high-affinity", "affinity": 0.91},
        ]

    monkeypatch.setattr(service, "field_affinity", fake_field_affinity)

    response = await service.search_researchers(db, "biology", page=1, size=1)

    assert response.search_type == "field"
    assert response.total == 2
    assert [item.researcher_id for item in response.items] == ["high-affinity"]
    assert response.items[0].relevance_score == 0.91
    assert db.execute_calls[0][0] is service._FIELD_SEARCH_SQL
    assert db.execute_calls[0][1] == {"pattern": "%biology%"}


def test_name_detection_sql_uses_exact_and_prefix_not_substring():
    count_sql = str(service._NAME_COUNT_SQL)
    name_sql = str(service._NAME_SEARCH_SQL)

    assert "= :norm_query" in count_sql
    assert "LIKE :norm_prefix" in count_sql
    assert "LIKE :norm_pattern" not in count_sql
    assert "LIKE :norm_pattern" not in name_sql


def test_recent_researcher_searches_use_separate_key_limit_and_dedupe(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(service, "get_redis", lambda db: fake)

    user_id = "user-1"
    for idx in range(7):
        service.save_recent_researcher_search(user_id, f"query-{idx}", "field")
    service.save_recent_researcher_search(user_id, "query-3", "name")

    key = "researcher_searches:user-1"
    assert key in fake.store
    assert all(call[0] == key for call in fake.set_calls)

    raw_items = json.loads(fake.store[key])
    assert len(raw_items) == 6
    assert raw_items[0]["query"] == "query-3"
    assert raw_items[0]["search_type"] == "name"
    assert [item["query"] for item in raw_items].count("query-3") == 1

    response = service.get_recent_researcher_searches(user_id)
    assert [item.query for item in response.items] == [item["query"] for item in raw_items]


def test_total_papers_sql_falls_back_to_corpus_paper_count_not_article_count():
    name_sql = str(service._NAME_SEARCH_SQL)
    field_sql = str(service._FIELD_SEARCH_SQL)

    assert "coalesce(r.total_papers, r.corpus_paper_count, 0) AS total_papers" in name_sql
    assert "coalesce(r.total_papers, r.corpus_paper_count, 0) AS total_papers" in field_sql
    assert "coalesce(r.total_papers, r.article_cnt" not in name_sql
    assert "coalesce(r.total_papers, r.article_cnt" not in field_sql
