import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.neo4j_client import get_neo4j_driver
from app.core.settings import settings
from app.integrations.semanticscholar.client import SemanticScholarClient
from app.integrations.semanticscholar.normalizer import (
    normalize_semantic_scholar_search_response,
)
from app.integrations.scienceon.client import ScienceOnClient
from app.integrations.scienceon.normalizer import normalize_scienceon_search_response
from app.integrations.scienceon.parser import parse_scienceon_xml
from app.repositories.graph_repository import GraphRepository
from app.schemas.search import (
    PaperSearchItem,
    SearchPapersRequest,
    SearchPapersResponse,
)
from app.services.credibility_service import enrich_items_with_credibility

DETAIL_API_PREFIX = "/api/v1/papers"
_SCIENCEON_CN_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{2,}\d{6,}$")


def _apply_basic_scoring(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    for item in items:
        score = 0.5

        if item.year:
            if item.year >= 2024:
                score += 0.2
            elif item.year >= 2021:
                score += 0.1

        if item.credibility.badge == "high":
            score += 0.2
        elif item.credibility.badge == "medium":
            score += 0.1

        score += min(len(item.keywords) * 0.03, 0.15)
        item.score = round(score, 2)

    return items


def _sort_items(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    return sorted(items, key=lambda x: x.score, reverse=True)


def _looks_like_doi(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower().startswith(("10.", "http://doi.org/", "https://doi.org/"))


def _build_detail_url(paper_cn: str) -> str:
    return f"{DETAIL_API_PREFIX}/{paper_cn}"


def _candidate_detail_paper_cn(item: PaperSearchItem) -> str | None:
    if item.source != "scienceon":
        return None

    paper_id = item.paper_id.strip()
    if not paper_id or paper_id.lower() == "unknown" or _looks_like_doi(paper_id):
        return None

    if _SCIENCEON_CN_PATTERN.match(paper_id):
        return paper_id

    return None


def _apply_detail_links(
    items: list[PaperSearchItem],
    existing_paper_cns: set[str],
) -> list[PaperSearchItem]:
    for item in items:
        candidate_cn = _candidate_detail_paper_cn(item)
        if candidate_cn and candidate_cn in existing_paper_cns:
            item.detail_available = True
            item.detail_paper_cn = candidate_cn
            item.detail_url = _build_detail_url(candidate_cn)
        else:
            item.detail_available = False
            item.detail_paper_cn = None
            item.detail_url = None
    return items


def _enrich_items_with_detail_links(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    candidate_cns = list(
        dict.fromkeys(
            candidate
            for item in items
            if (candidate := _candidate_detail_paper_cn(item)) is not None
        )
    )
    if not candidate_cns:
        return _apply_detail_links(items, set())

    try:
        driver = get_neo4j_driver()
        try:
            repository = GraphRepository(driver)
            existing_cns = repository.find_existing_paper_cns(candidate_cns)
        finally:
            driver.close()
    except Exception:
        existing_cns = set()

    return _apply_detail_links(items, existing_cns)


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


async def search_papers_service(
    request: SearchPapersRequest,
    db: AsyncSession | None = None,
) -> SearchPapersResponse:
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

    if db is not None:
        try:
            items = await enrich_items_with_credibility(items, db)
        except Exception:
            pass

    items = _enrich_items_with_detail_links(items)
    items = _apply_basic_scoring(items)
    items = _sort_items(items)

    return SearchPapersResponse(
        search_id="search_combined_001",
        items=items[: request.size],
    )
