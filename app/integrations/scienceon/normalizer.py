from __future__ import annotations
from app.schemas.search import PaperAuthor, PaperSearchItem
from app.services.credibility_service import calculate_credibility


def _split_semicolon_text(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(";") if item.strip()]


def _split_dot_text(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(".") if item.strip()]


def _first_value(record: dict, keys: list[str]):
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _parse_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _find_records(node):
    if isinstance(node, list):
        return node

    if not isinstance(node, dict):
        return []

    candidate_keys = [
        "record",
        "Record",
        "records",
        "Records",
        "result",
        "RESULT",
        "outputData",
        "resultSet",
        "returnObject",
    ]

    for key in candidate_keys:
        value = node.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _find_records(value)
            if nested:
                return nested

    for value in node.values():
        if isinstance(value, dict):
            nested = _find_records(value)
            if nested:
                return nested
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            return value

    return []


def normalize_scienceon_search_response(parsed: dict) -> list[PaperSearchItem]:
    records = _find_records(parsed)
    items: list[PaperSearchItem] = []

    for record in records:
        if not isinstance(record, dict):
            continue

        authors_raw = _split_semicolon_text(record.get("Author"))
        affiliations_raw = _split_semicolon_text(record.get("Affiliation"))
        keywords_raw = _split_dot_text(record.get("Keyword"))

        authors = []
        for idx, author_name in enumerate(authors_raw):
            affiliation = affiliations_raw[idx] if idx < len(affiliations_raw) else None
            authors.append(PaperAuthor(name=author_name, affiliation=affiliation))

        pubyear = record.get("Pubyear")
        try:
            year = int(pubyear) if pubyear else None
        except ValueError:
            year = None

        journal_name = record.get("JournalName")
        citation_count = _parse_int(
            _first_value(
                record,
                [
                    "CitationCount",
                    "citationCount",
                    "CitedCount",
                    "CitedByCount",
                    "CitedCnt",
                ],
            )
        )
        item = PaperSearchItem(
            paper_id=record.get("CN") or record.get("DOI") or "unknown",
            title=record.get("Title") or record.get("Title2") or "Untitled",
            authors=authors,
            year=year,
            abstract=record.get("Abstract") or record.get("Abstract2"),
            keywords=keywords_raw,
            journal_name=journal_name,
            issn=record.get("ISSN"),
            source="scienceon",
            credibility=calculate_credibility(
                citation_count=citation_count,
                journal_name=journal_name,
                kci_hint=record.get("DBCode") == "JAKO",
            ),
            score=0.0,
        )
        items.append(item)

    return items
