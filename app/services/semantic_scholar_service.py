from app.integrations.semanticscholar.client import SemanticScholarClient
from app.integrations.semanticscholar.normalizer import (
    normalize_semantic_scholar_search_response,
)
from app.schemas.search import SearchPapersResponse


async def semantic_scholar_search_service(query: str, size: int = 10) -> SearchPapersResponse:
    client = SemanticScholarClient()
    payload = await client.search_papers(query=query, limit=size)
    items = normalize_semantic_scholar_search_response(payload)

    return SearchPapersResponse(
        search_id="search_semantic_001",
        items=items,
    )