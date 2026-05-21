from app.schemas.search import PaperAuthor, PaperSearchItem
from app.services.credibility_service import calculate_credibility


def _extract_journal_name(record: dict) -> str | None:
    journal = record.get("journal")
    if isinstance(journal, dict):
        journal_name = journal.get("name")
        if journal_name:
            return journal_name

    venue = record.get("venue")
    if isinstance(venue, str) and venue.strip():
        return venue.strip()

    return None


def normalize_semantic_scholar_search_response(payload: dict) -> list[PaperSearchItem]:
    data = payload.get("data", [])
    items: list[PaperSearchItem] = []

    for record in data:
        authors = [
            PaperAuthor(
                name=author.get("name", "Unknown"),
                affiliation=None,
            )
            for author in record.get("authors", [])
        ]

        citation_count = record.get("citationCount")
        journal_name = _extract_journal_name(record)
        item = PaperSearchItem(
            paper_id=record.get("paperId", "unknown"),
            title=record.get("title", "Untitled"),
            authors=authors,
            year=record.get("year"),
            abstract=record.get("abstract"),
            keywords=[],
            journal_name=journal_name,
            issn=None,
            source="semantic_scholar",
            credibility=calculate_credibility(
                citation_count=citation_count,
                journal_name=journal_name,
            ),
            score=0.0,
        )
        items.append(item)

    return items
