import json

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


def test_build_researcher_graph_returns_only_field_to_researcher_edges():
    items = [
        _researcher(
            "kci:one",
            name="Researcher One",
            keywords=["biology", "shared"],
            total_citations=12,
            citation_source="kci",
            field_paper_count=2,
            relevance_score=4.0,
        ),
        _researcher(
            "oa:two",
            name="Researcher Two",
            keywords=["chemistry", "shared"],
            total_citations=34,
            citation_source="openalex",
            field_paper_count=1,
            relevance_score=2.0,
        ),
    ]

    graph = service.build_researcher_graph("biology", items)

    assert graph.query == "biology"
    assert len(graph.nodes) == 3
    assert len(graph.edges) == 2
    assert {edge.edge_type for edge in graph.edges} == {"field_relevance"}
    assert {edge.source for edge in graph.edges} == {"field:biology"}
    assert {edge.target for edge in graph.edges} == {"researcher:kci:one", "researcher:oa:two"}

    researcher_nodes = [node for node in graph.nodes if node.node_type == "researcher"]
    assert researcher_nodes[0].citation_source == "kci"
    assert researcher_nodes[1].citation_source == "openalex"


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
