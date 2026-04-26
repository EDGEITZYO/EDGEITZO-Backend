from app.integrations.scienceon.client import ScienceOnClient
from app.integrations.scienceon.normalizer import normalize_scienceon_search_response
from app.integrations.scienceon.parser import parse_scienceon_xml
from app.schemas.search import SearchPapersResponse


async def scienceon_search_service(query: str, page: int = 1, size: int = 10) -> SearchPapersResponse:
    client = ScienceOnClient()
    raw_xml = await client.search_articles(query=query, page=page, size=size)
    parsed = parse_scienceon_xml(raw_xml)
    items = normalize_scienceon_search_response(parsed)

    return SearchPapersResponse(
        search_id="scienceon_search_demo",
        items=items,
    )


async def scienceon_raw_service(query: str, page: int = 1, size: int = 3) -> dict:
    client = ScienceOnClient()
    raw_xml = await client.search_articles(query=query, page=page, size=size)
    parsed = parse_scienceon_xml(raw_xml)

    return {
        "raw_xml_preview": raw_xml[:3000],
        "parsed_preview": parsed,
    }