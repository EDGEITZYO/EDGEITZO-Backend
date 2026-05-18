from typing import Optional

from fastapi import HTTPException
from starlette import status

from app.core.neo4j_client import get_neo4j_driver
from app.repositories.graph_repository import GraphRepository
from app.schemas.graph import GraphKeywordEdge, GraphKeywordNode, KeywordGraphResponse


def _build_keyword_graph_response(
    center: dict,
    related_items: list[dict],
) -> KeywordGraphResponse:
    center_node = GraphKeywordNode(**center)
    related_nodes = [GraphKeywordNode(**item["node"]) for item in related_items]
    edges = [GraphKeywordEdge(**item["edge"]) for item in related_items]

    return KeywordGraphResponse(
        center=center_node,
        nodes=[center_node, *related_nodes],
        edges=edges,
    )


def get_keyword_graph_service(
    keyword: str,
    *,
    lang: Optional[str] = None,
    limit: int = 20,
    min_paper_count: int = 1,
) -> KeywordGraphResponse:
    normalized_keyword = keyword.strip()
    if not normalized_keyword:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="keyword must not be empty",
        )

    driver = get_neo4j_driver()
    try:
        repository = GraphRepository(driver)
        center = repository.find_keyword(normalized_keyword, lang=lang)
        if center is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"keyword not found: {normalized_keyword}",
            )

        related_items = repository.find_related_keywords(
            center["key"],
            limit=limit,
            min_paper_count=min_paper_count,
        )
        return _build_keyword_graph_response(center, related_items)
    finally:
        driver.close()


def expand_keyword_graph_service(
    keyword_key: str,
    *,
    limit: int = 20,
    min_paper_count: int = 1,
) -> KeywordGraphResponse:
    return get_keyword_graph_service(
        keyword_key,
        limit=limit,
        min_paper_count=min_paper_count,
    )
