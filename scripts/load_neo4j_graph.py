"""Load normalized ScienceON paper metadata into Neo4j."""
from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_NEO4J_USER = "neo4j"
DEFAULT_NEO4J_PASSWORD = "password1234"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(DEFAULT_ENV_PATH)


@dataclass
class GraphPayload:
    papers: list[dict[str, Any]]
    keywords: list[dict[str, Any]]
    paper_keywords: list[dict[str, Any]]
    authors: list[dict[str, Any]]
    paper_authors: list[dict[str, Any]]
    journals: list[dict[str, Any]]
    paper_journals: list[dict[str, Any]]
    years: list[dict[str, Any]]
    paper_years: list[dict[str, Any]]
    related_keywords: list[dict[str, Any]]
    skipped_papers: int


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = unicodedata.normalize("NFKC", str(value))
    cleaned = " ".join(cleaned.split()).strip()
    return cleaned or None


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]

    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        cleaned = _clean_text(item)
        if not cleaned:
            continue

        key = cleaned.casefold()
        if key in seen:
            continue

        seen.add(key)
        result.append(cleaned)
    return result


def _to_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _keyword_key(name: str, lang: str) -> str:
    return f"{lang}:{name.casefold()}"


def _build_keyword_rows(
    paper_cn: str,
    keywords: list[str],
    *,
    lang: str,
    source_field: str,
    loaded_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keyword_rows: list[dict[str, Any]] = []
    rel_rows: list[dict[str, Any]] = []

    for keyword in keywords:
        key = _keyword_key(keyword, lang)
        keyword_rows.append(
            {
                "key": key,
                "name": keyword,
                "normalized_name": keyword.casefold(),
                "lang": lang,
                "source_field": source_field,
                "loaded_at": loaded_at,
            }
        )
        rel_rows.append(
            {
                "paper_cn": paper_cn,
                "keyword_key": key,
                "lang": lang,
                "source_field": source_field,
                "loaded_at": loaded_at,
            }
        )

    return keyword_rows, rel_rows


def _merge_unique(rows: list[dict[str, Any]], key_field: str) -> list[dict[str, Any]]:
    unique: dict[Any, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row[key_field], row)
    return list(unique.values())


def build_graph_payload(data: dict[str, Any], *, limit: int | None = None) -> GraphPayload:
    raw_papers = data.get("papers")
    if not isinstance(raw_papers, list):
        raise ValueError("Input JSON must contain a 'papers' list.")

    if limit is not None:
        raw_papers = raw_papers[:limit]

    loaded_at = datetime.now().isoformat(timespec="seconds")

    paper_rows: list[dict[str, Any]] = []
    keyword_rows: list[dict[str, Any]] = []
    paper_keyword_rows: list[dict[str, Any]] = []
    author_rows: list[dict[str, Any]] = []
    paper_author_rows: list[dict[str, Any]] = []
    journal_rows: list[dict[str, Any]] = []
    paper_journal_rows: list[dict[str, Any]] = []
    year_rows: list[dict[str, Any]] = []
    paper_year_rows: list[dict[str, Any]] = []
    related_counter: Counter[tuple[str, str, str]] = Counter()
    skipped_papers = 0

    for paper in raw_papers:
        if not isinstance(paper, dict):
            skipped_papers += 1
            continue

        cn = _clean_text(paper.get("CN"))
        if not cn:
            skipped_papers += 1
            continue

        issn = _string_list(paper.get("ISSN")) or None
        pubyear = _to_int(paper.get("Pubyear"))
        journal_name = _clean_text(paper.get("JournalName"))

        paper_rows.append(
            {
                "cn": cn,
                "db_code": _clean_text(paper.get("DBCode")),
                "title": _clean_text(paper.get("Title")),
                "title_en": _clean_text(paper.get("Title2")),
                "abstract": _clean_text(paper.get("Abstract")),
                "abstract_en": _clean_text(paper.get("Abstract2")),
                "doi": _clean_text(paper.get("DOI")),
                "pubyear": pubyear,
                "journal_name": journal_name,
                "issn": issn,
                "keyword_raw": _clean_text(paper.get("keyword_raw")),
                "keyword2_raw": _clean_text(paper.get("keyword2_raw")),
                "loaded_at": loaded_at,
            }
        )

        ko_keywords = _string_list(paper.get("Keyword"))
        en_keywords = _string_list(paper.get("Keyword2"))

        ko_keyword_rows, ko_rel_rows = _build_keyword_rows(
            cn,
            ko_keywords,
            lang="ko",
            source_field="Keyword",
            loaded_at=loaded_at,
        )
        en_keyword_rows, en_rel_rows = _build_keyword_rows(
            cn,
            en_keywords,
            lang="en",
            source_field="Keyword2",
            loaded_at=loaded_at,
        )
        keyword_rows.extend(ko_keyword_rows)
        keyword_rows.extend(en_keyword_rows)
        paper_keyword_rows.extend(ko_rel_rows)
        paper_keyword_rows.extend(en_rel_rows)

        paper_keyword_keys = sorted({row["keyword_key"] for row in ko_rel_rows + en_rel_rows})
        for from_key, to_key in combinations(paper_keyword_keys, 2):
            from_lang = from_key.split(":", 1)[0]
            to_lang = to_key.split(":", 1)[0]
            lang_pair = "-".join(sorted((from_lang, to_lang)))
            related_counter[(from_key, to_key, lang_pair)] += 1

        for index, author_name in enumerate(_string_list(paper.get("Author")), start=1):
            author_rows.append({"name": author_name, "loaded_at": loaded_at})
            paper_author_rows.append(
                {
                    "paper_cn": cn,
                    "author_name": author_name,
                    "order": index,
                    "loaded_at": loaded_at,
                }
            )

        if journal_name:
            journal_rows.append({"name": journal_name, "loaded_at": loaded_at})
            paper_journal_rows.append(
                {"paper_cn": cn, "journal_name": journal_name, "loaded_at": loaded_at}
            )

        if pubyear:
            year_rows.append({"value": pubyear, "loaded_at": loaded_at})
            paper_year_rows.append({"paper_cn": cn, "year": pubyear, "loaded_at": loaded_at})

    related_keyword_rows = [
        {
            "from_key": from_key,
            "to_key": to_key,
            "lang_pair": lang_pair,
            "paper_count": paper_count,
            "loaded_at": loaded_at,
        }
        for (from_key, to_key, lang_pair), paper_count in related_counter.items()
    ]

    return GraphPayload(
        papers=paper_rows,
        keywords=_merge_unique(keyword_rows, "key"),
        paper_keywords=paper_keyword_rows,
        authors=_merge_unique(author_rows, "name"),
        paper_authors=paper_author_rows,
        journals=_merge_unique(journal_rows, "name"),
        paper_journals=paper_journal_rows,
        years=_merge_unique(year_rows, "value"),
        paper_years=paper_year_rows,
        related_keywords=related_keyword_rows,
        skipped_papers=skipped_papers,
    )


def _run_write_batch(session: Any, query: str, rows: list[dict[str, Any]], batch_size: int) -> None:
    def write_batch(tx: Any, batch: list[dict[str, Any]]) -> None:
        tx.run(query, rows=batch).consume()

    for start in range(0, len(rows), batch_size):
        session.execute_write(write_batch, rows[start : start + batch_size])


def _run_statement(session: Any, query: str) -> None:
    def write_statement(tx: Any) -> None:
        tx.run(query).consume()

    session.execute_write(write_statement)


def create_schema(session: Any) -> None:
    statements = [
        "CREATE CONSTRAINT paper_cn_unique IF NOT EXISTS FOR (p:Paper) REQUIRE p.cn IS UNIQUE",
        "CREATE CONSTRAINT keyword_key_unique IF NOT EXISTS FOR (k:Keyword) REQUIRE k.key IS UNIQUE",
        "CREATE CONSTRAINT author_name_unique IF NOT EXISTS FOR (a:Author) REQUIRE a.name IS UNIQUE",
        "CREATE CONSTRAINT journal_name_unique IF NOT EXISTS FOR (j:Journal) REQUIRE j.name IS UNIQUE",
        "CREATE CONSTRAINT year_value_unique IF NOT EXISTS FOR (y:Year) REQUIRE y.value IS UNIQUE",
        "CREATE INDEX paper_pubyear_index IF NOT EXISTS FOR (p:Paper) ON (p.pubyear)",
        "CREATE INDEX keyword_lang_index IF NOT EXISTS FOR (k:Keyword) ON (k.lang)",
        "CREATE FULLTEXT INDEX paper_text_fulltext IF NOT EXISTS FOR (p:Paper) ON EACH [p.title, p.title_en, p.abstract, p.abstract_en]",
        "CREATE FULLTEXT INDEX keyword_name_fulltext IF NOT EXISTS FOR (k:Keyword) ON EACH [k.name, k.normalized_name]",
    ]
    for statement in statements:
        _run_statement(session, statement)


def reset_graph(session: Any) -> None:
    _run_statement(session, "MATCH (n) DETACH DELETE n")


def load_payload(session: Any, payload: GraphPayload, *, batch_size: int) -> None:
    paper_query = """
    UNWIND $rows AS row
    MERGE (p:Paper {cn: row.cn})
    SET p.db_code = row.db_code,
        p.title = row.title,
        p.title_en = row.title_en,
        p.abstract = row.abstract,
        p.abstract_en = row.abstract_en,
        p.doi = row.doi,
        p.pubyear = row.pubyear,
        p.journal_name = row.journal_name,
        p.issn = row.issn,
        p.keyword_raw = row.keyword_raw,
        p.keyword2_raw = row.keyword2_raw,
        p.loaded_at = row.loaded_at
    """

    keyword_query = """
    UNWIND $rows AS row
    MERGE (k:Keyword {key: row.key})
    SET k.name = row.name,
        k.normalized_name = row.normalized_name,
        k.lang = row.lang,
        k.source_field = row.source_field,
        k.loaded_at = row.loaded_at
    """

    paper_keyword_query = """
    UNWIND $rows AS row
    MATCH (p:Paper {cn: row.paper_cn})
    MATCH (k:Keyword {key: row.keyword_key})
    MERGE (p)-[r:HAS_KEYWORD]->(k)
    SET r.lang = row.lang,
        r.source_field = row.source_field,
        r.loaded_at = row.loaded_at
    """

    author_query = """
    UNWIND $rows AS row
    MERGE (a:Author {name: row.name})
    SET a.loaded_at = row.loaded_at
    """

    paper_author_query = """
    UNWIND $rows AS row
    MATCH (p:Paper {cn: row.paper_cn})
    MATCH (a:Author {name: row.author_name})
    MERGE (a)-[r:AUTHORED]->(p)
    SET r.order = row.order,
        r.loaded_at = row.loaded_at
    """

    journal_query = """
    UNWIND $rows AS row
    MERGE (j:Journal {name: row.name})
    SET j.loaded_at = row.loaded_at
    """

    paper_journal_query = """
    UNWIND $rows AS row
    MATCH (p:Paper {cn: row.paper_cn})
    MATCH (j:Journal {name: row.journal_name})
    MERGE (p)-[r:PUBLISHED_IN]->(j)
    SET r.loaded_at = row.loaded_at
    """

    year_query = """
    UNWIND $rows AS row
    MERGE (y:Year {value: row.value})
    SET y.loaded_at = row.loaded_at
    """

    paper_year_query = """
    UNWIND $rows AS row
    MATCH (p:Paper {cn: row.paper_cn})
    MATCH (y:Year {value: row.year})
    MERGE (p)-[r:PUBLISHED_IN_YEAR]->(y)
    SET r.loaded_at = row.loaded_at
    """

    related_keyword_query = """
    UNWIND $rows AS row
    MATCH (from:Keyword {key: row.from_key})
    MATCH (to:Keyword {key: row.to_key})
    MERGE (from)-[r:RELATED_TO]->(to)
    SET r.paper_count = row.paper_count,
        r.lang_pair = row.lang_pair,
        r.loaded_at = row.loaded_at
    """

    for label, query, rows in [
        ("Paper", paper_query, payload.papers),
        ("Keyword", keyword_query, payload.keywords),
        ("Paper-HAS_KEYWORD-Keyword", paper_keyword_query, payload.paper_keywords),
        ("Author", author_query, payload.authors),
        ("Author-AUTHORED-Paper", paper_author_query, payload.paper_authors),
        ("Journal", journal_query, payload.journals),
        ("Paper-PUBLISHED_IN-Journal", paper_journal_query, payload.paper_journals),
        ("Year", year_query, payload.years),
        ("Paper-PUBLISHED_IN_YEAR-Year", paper_year_query, payload.paper_years),
        ("Keyword-RELATED_TO-Keyword", related_keyword_query, payload.related_keywords),
    ]:
        if not rows:
            continue
        _run_write_batch(session, query, rows, batch_size)
        print(f"loaded {label}: {len(rows)}")


def print_summary(payload: GraphPayload) -> None:
    print(f"papers: {len(payload.papers)}")
    print(f"keywords: {len(payload.keywords)}")
    print(f"paper_keyword_edges: {len(payload.paper_keywords)}")
    print(f"authors: {len(payload.authors)}")
    print(f"paper_author_edges: {len(payload.paper_authors)}")
    print(f"journals: {len(payload.journals)}")
    print(f"paper_journal_edges: {len(payload.paper_journals)}")
    print(f"years: {len(payload.years)}")
    print(f"paper_year_edges: {len(payload.paper_years)}")
    print(f"related_keyword_edges: {len(payload.related_keywords)}")
    print(f"skipped_papers: {payload.skipped_papers}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load normalized ScienceON data into Neo4j.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=None, help="Load only the first N papers.")
    parser.add_argument("--dry-run", action="store_true", help="Build rows and print counts only.")
    parser.add_argument("--reset", action="store_true", help="Delete all Neo4j nodes before loading.")
    parser.add_argument("--uri", default=os.getenv("NEO4J_URI", DEFAULT_NEO4J_URI))
    parser.add_argument(
        "--user",
        default=os.getenv("NEO4J_USER") or os.getenv("NEO4J_USERNAME", DEFAULT_NEO4J_USER),
    )
    parser.add_argument("--password", default=os.getenv("NEO4J_PASSWORD", DEFAULT_NEO4J_PASSWORD))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve()
    if not input_path.exists():
        print(f"[error] input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(input_path.read_text(encoding="utf-8-sig"))
    payload = build_graph_payload(data, limit=args.limit)

    print(f"input: {input_path}")
    print_summary(payload)
    if args.dry_run:
        return

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        with driver.session() as session:
            if args.reset:
                print("resetting Neo4j graph...")
                reset_graph(session)
            create_schema(session)
            load_payload(session, payload, batch_size=args.batch_size)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
