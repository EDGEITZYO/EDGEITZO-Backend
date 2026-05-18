from typing import Optional

from fastapi import HTTPException
from starlette import status

from app.core.neo4j_client import get_neo4j_driver
from app.repositories.graph_repository import GraphRepository
from app.schemas.graph import (
    GraphKeywordEdge,
    GraphKeywordNode,
    GraphPaperItem,
    KeywordGraphResponse,
    KeywordPapersResponse,
    PaperGraphEdge,
    PaperGraphNode,
    PaperNeighborGraphResponse,
)


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


def get_keyword_papers_service(
    keyword_key: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> KeywordPapersResponse:
    normalized_keyword_key = keyword_key.strip()
    if not normalized_keyword_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="keyword_key must not be empty",
        )

    driver = get_neo4j_driver()
    try:
        repository = GraphRepository(driver)
        result = repository.find_papers_by_keyword(
            normalized_keyword_key,
            limit=limit,
            offset=offset,
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"keyword not found: {normalized_keyword_key}",
            )

        return KeywordPapersResponse(
            keyword=GraphKeywordNode(**result["keyword"]),
            total_count=result["total_count"],
            items=[GraphPaperItem(**paper) for paper in result["papers"]],
        )
    finally:
        driver.close()


def get_paper_neighbor_graph_service(
    paper_cn: str,
    *,
    keyword_limit: int = 30,
) -> PaperNeighborGraphResponse:
    normalized_paper_cn = paper_cn.strip()
    if not normalized_paper_cn:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="paper_cn must not be empty",
        )

    driver = get_neo4j_driver()
    try:
        repository = GraphRepository(driver)
        result = repository.find_paper_neighbor_graph(
            normalized_paper_cn,
            keyword_limit=keyword_limit,
        )
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"paper not found: {normalized_paper_cn}",
            )

        return PaperNeighborGraphResponse(
            center=PaperGraphNode(**result["center"]),
            nodes=[PaperGraphNode(**node) for node in result["nodes"]],
            edges=[PaperGraphEdge(**edge) for edge in result["edges"]],
        )
    finally:
        driver.close()
