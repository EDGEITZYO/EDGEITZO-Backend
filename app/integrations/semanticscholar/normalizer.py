from app.schemas.search import (
    CredibilityInfo,
    PaperAuthor,
    PaperSearchItem,
)


def normalize_semantic_scholar_search_response(payload: dict) -> list[PaperSearchItem]:
    data = payload.get("data", [])
    items: list[PaperSearchItem] = []

    for record in data:
        authors = []
        for author in record.get("authors", []):
            authors.append(
                PaperAuthor(
                    name=author.get("name", "Unknown"),
                    affiliation=None,
                )
            )

        citation_count = record.get("citationCount")

        badge = "unknown"
        if citation_count is not None:
            if citation_count >= 50:
                badge = "high"
            elif citation_count >= 10:
                badge = "medium"
            else:
                badge = "low"

        item = PaperSearchItem(
            paper_id=record.get("paperId", "unknown"),
            title=record.get("title", "제목 없음"),
            authors=authors,
            year=record.get("year"),
            abstract=record.get("abstract"),
            keywords=[],
            journal_name=None,
            source="semantic_scholar",
            credibility=CredibilityInfo(
                badge=badge,
                citation_count=citation_count,
                impact_factor=None,
                kci_registered=False,
                summary="Semantic Scholar 검색 결과",
            ),
            score=0.0,
        )
        items.append(item)

    return items