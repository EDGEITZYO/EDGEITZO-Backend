from app.core.settings import settings
from app.integrations.semanticscholar.client import SemanticScholarClient
from app.integrations.semanticscholar.normalizer import (
    normalize_semantic_scholar_search_response,
)
from app.integrations.scienceon.client import ScienceOnClient
from app.integrations.scienceon.normalizer import normalize_scienceon_search_response
from app.integrations.scienceon.parser import parse_scienceon_xml
from app.schemas.search import (
    PaperSearchItem,
    SearchPapersRequest,
    SearchPapersResponse,
)


def _apply_basic_scoring(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    for item in items:
        score = 0.5

        if item.year:
            if item.year >= 2024:
                score += 0.2
            elif item.year >= 2021:
                score += 0.1

        if item.credibility.citation_count:
            if item.credibility.citation_count >= 50:
                score += 0.2
            elif item.credibility.citation_count >= 10:
                score += 0.1

        score += min(len(item.keywords) * 0.03, 0.15)
        item.score = round(score, 2)

    return items


def _sort_items(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    return sorted(items, key=lambda x: x.score, reverse=True)


async def _search_semantic_scholar(query: str, size: int) -> list[PaperSearchItem]:
    try:
        client = SemanticScholarClient()
        payload = await client.search_papers(query=query, limit=size)
        return normalize_semantic_scholar_search_response(payload)
    except Exception:
        return []


async def _search_scienceon_if_available(query: str, page: int, size: int) -> list[PaperSearchItem]:
    if not settings.scienceon_client_id or not settings.scienceon_token:
        return []

    try:
        client = ScienceOnClient()
        raw_xml = await client.search_articles(query=query, page=page, size=size)
        parsed = parse_scienceon_xml(raw_xml)

        metadata = parsed.get("MetaData", {})
        status_code = metadata.get("resultSummary", {}).get("statusCode")
        if status_code and status_code != "200":
            return []

        return normalize_scienceon_search_response(parsed)
    except Exception:
        return []


async def search_papers_service(request: SearchPapersRequest) -> SearchPapersResponse:
    items: list[PaperSearchItem] = []

    semantic_items = await _search_semantic_scholar(
        query=request.query,
        size=request.size,
    )
    items.extend(semantic_items)

    scienceon_items = await _search_scienceon_if_available(
        query=request.query,
        page=request.page,
        size=request.size,
    )
    items.extend(scienceon_items)

    items = _apply_basic_scoring(items)
    items = _sort_items(items)

    return SearchPapersResponse(
        search_id="search_combined_001",
        items=items[: request.size],
    )