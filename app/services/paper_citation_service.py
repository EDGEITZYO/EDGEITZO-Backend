from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.core.neo4j_client import get_neo4j_driver
from app.core.redis import get_redis
from app.core.settings import settings
from app.models.journal import Journal
from app.models.paper import Paper, PaperCitationExternalRef
from app.repositories import paper_citation_repository
from app.repositories.graph_repository import GraphRepository
from app.repositories.paper_repository import get_paper_cards_batch
from app.schemas.paper import PaperCardTrustBadge
from app.schemas.paper_citation import (
    PaperCitationCard,
    PaperCitationEdge,
    PaperCitationExpandResponse,
    PaperCitationGraphResponse,
    PaperCitationNode,
)

logger = logging.getLogger(__name__)

_REDIS_DB = 7

Direction = Literal["reference", "citing"]

# db_code -> 기본 논문유형 레이블 (app/services/paper_filter_service._DB_CODE_DEFAULT_LABEL과 동일 규칙).
_DB_CODE_DEFAULT_LABEL: dict[str, str] = {
    "JAKO": "학술 저널",
    "JAFO": "학술 저널",
    "DIKO": "학위논문",
    "CFKO": "학술 저널",
    "CFFO": "학술 저널",
}


def _citation_edge_for(direction: Direction, anchor_key: str, other_key: str) -> PaperCitationEdge:
    """direction="reference"면 anchor가 인용한 것(anchor->other), "citing"이면 other가 anchor를 인용한 것(other->anchor)."""
    if direction == "reference":
        return PaperCitationEdge(source=anchor_key, target=other_key)
    return PaperCitationEdge(source=other_key, target=anchor_key)


def _node_from_in_service(paper: dict[str, Any], *, tier: int, side: str, has_more: bool = False) -> PaperCitationNode:
    cn = paper["cn"]
    return PaperCitationNode(
        key=cn,
        in_service=True,
        paper_id=cn,
        title=paper.get("title"),
        title_en=paper.get("title_en"),
        pubyear=paper.get("pubyear"),
        tier=tier,
        side=side,
        has_more=has_more,
    )


def _node_from_external(ref: PaperCitationExternalRef, *, tier: int, side: str) -> PaperCitationNode:
    return PaperCitationNode(
        key=ref.external_id,
        in_service=False,
        paper_id=None,
        title=ref.title,
        title_en=None,
        pubyear=ref.pubyear,
        tier=tier,
        side=side,
        has_more=False,  # 외부 논문은 상세페이지/그래프 데이터가 없어 확장 불가
    )


# ---------------------------------------------------------------------------
# in-service(Neo4j) 부분 — sync, Redis 캐시 대상 (외부노드 병합 전 단계까지만)
# ---------------------------------------------------------------------------

@dataclass
class _InServicePartial:
    center: PaperCitationNode
    nodes: list[PaperCitationNode]
    edges: list[PaperCitationEdge]
    has_more_in_service: bool


def _assign_keyword_clusters(repo: GraphRepository, nodes: list[PaperCitationNode]) -> None:
    """07-01: 요약 그래프의 1단계 in-service 자식끼리 키워드를 일정 개수 이상 공유하면
    같은 cluster_id를 부여 (union-find). 2명 이상 묶인 그룹에만 id를 매기고,
    아무와도 안 묶이는 단독 노드는 cluster_id=None으로 남긴다."""
    cns = [n.key for n in nodes if n.in_service]
    if len(cns) < 2:
        return

    pairs = repo.find_keyword_overlap_pairs(cns, min_shared=settings.paper_citation_cluster_min_shared_keywords)
    if not pairs:
        return

    parent: dict[str, str] = {cn: cn for cn in cns}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for pair in pairs:
        union(pair["a"], pair["b"])

    # 크기 2 이상인 그룹에만 순차적으로 정수 id 부여
    root_to_cluster_id: dict[str, int] = {}
    group_sizes: dict[str, int] = {}
    for cn in cns:
        group_sizes[find(cn)] = group_sizes.get(find(cn), 0) + 1

    next_id = 1
    for cn in cns:
        root = find(cn)
        if group_sizes[root] < 2:
            continue
        if root not in root_to_cluster_id:
            root_to_cluster_id[root] = next_id
            next_id += 1

    by_key = {n.key: n for n in nodes}
    for cn in cns:
        cluster_id = root_to_cluster_id.get(find(cn))
        if cluster_id is not None:
            by_key[cn].cluster_id = cluster_id


def _build_in_service_part_sync(cn: str, direction: Direction, limit: int) -> Optional[_InServicePartial]:
    driver = get_neo4j_driver()
    try:
        repo = GraphRepository(driver)
        center_dict = repo.find_paper(cn)
        if center_dict is None:
            return None

        center_node = _node_from_in_service(center_dict, tier=0, side="center")
        neighbors = repo.find_citation_neighbors(cn, direction=direction, limit=limit)
        has_more_in_service = repo.has_more_citation_neighbors(
            cn, direction=direction, excluded_cns=[n["cn"] for n in neighbors] + [cn]
        )

        placed = {cn} | {n["cn"] for n in neighbors}
        nodes = [_node_from_in_service(n, tier=1, side="child") for n in neighbors]
        edges = [_citation_edge_for(direction, cn, n["cn"]) for n in neighbors]

        for node, n in zip(nodes, neighbors):
            node.has_more = repo.has_more_citation_neighbors(n["cn"], direction=direction, excluded_cns=list(placed))

        # 07-01: 요약 그래프의 1단계 자식끼리 키워드 공유 기반 클러스터링 (expand로 추가된
        # 노드에는 적용 안 함 — 명세가 요약 그래프에만 요구).
        _assign_keyword_clusters(repo, nodes)

        return _InServicePartial(center=center_node, nodes=nodes, edges=edges, has_more_in_service=has_more_in_service)
    finally:
        driver.close()


def _cache_key(cn: str, direction: Direction) -> str:
    return f"paper_citation:subgraph:in_service:{direction}:{cn}"


def _serialize_partial(result: _InServicePartial) -> str:
    return json.dumps(
        {
            "center": result.center.model_dump(),
            "nodes": [n.model_dump() for n in result.nodes],
            "edges": [e.model_dump() for e in result.edges],
            "has_more_in_service": result.has_more_in_service,
        },
        ensure_ascii=False,
    )


def _deserialize_partial(raw: str) -> _InServicePartial:
    data = json.loads(raw)
    return _InServicePartial(
        center=PaperCitationNode(**data["center"]),
        nodes=[PaperCitationNode(**n) for n in data["nodes"]],
        edges=[PaperCitationEdge(**e) for e in data["edges"]],
        has_more_in_service=data["has_more_in_service"],
    )


def _get_or_build_in_service_part(cn: str, direction: Direction) -> Optional[_InServicePartial]:
    cache_key = _cache_key(cn, direction)
    try:
        r = get_redis(_REDIS_DB)
        cached = r.get(cache_key)
        if cached:
            return _deserialize_partial(cached)
    except Exception:
        logger.warning("인용관계 그래프 캐시 조회 실패", exc_info=True)

    result = _build_in_service_part_sync(cn, direction, settings.paper_citation_summary_limit)
    if result is None:
        return None

    try:
        r = get_redis(_REDIS_DB)
        r.set(cache_key, _serialize_partial(result), ex=settings.paper_citation_cache_ttl_seconds)
    except Exception:
        logger.warning("인용관계 그래프 캐시 저장 실패", exc_info=True)

    return result


# ---------------------------------------------------------------------------
# 카드 빌더
# ---------------------------------------------------------------------------

async def _build_in_service_cards(db: AsyncSession, cns: list[str]) -> dict[str, PaperCitationCard]:
    """papers 테이블 조인으로 in-service 카드 구성. ChromaDB는 안 거침(그래프 노드는 이미
    자체 코퍼스 소속이 확정돼 있으므로 시맨틱 검색이 불필요)."""
    if not cns:
        return {}

    result = await db.execute(
        select(Paper, Journal.sci_indexed)
        .outerjoin(Journal, Paper.journal_id == Journal.id)
        .where(Paper.id.in_(cns))
    )
    rows = {row[0].id: (row[0], row[1]) for row in result.all()}
    extras = await get_paper_cards_batch(db, cns)

    cards: dict[str, PaperCitationCard] = {}
    for cn in cns:
        entry = rows.get(cn)
        if entry is None:
            continue
        paper, sci_indexed = entry
        extra = extras.get(cn, {})

        degree_type: Optional[str] = None
        card_paper_type = _DB_CODE_DEFAULT_LABEL.get(paper.db_code or "")
        if (paper.db_code or "") == "DIKO":
            degree = paper.degree or ""
            if "박사" in degree:
                degree_type = "박사학위 논문"
                card_paper_type = "박사학위 논문"
            elif "석사" in degree:
                degree_type = "석사학위 논문"
                card_paper_type = "석사학위 논문"
            else:
                degree_type = "학위논문"

        trust_badge = PaperCardTrustBadge(
            kci=extra.get("kci_registered", paper.db_code == "JAKO"),
            sci=bool(sci_indexed) if sci_indexed is not None else False,
            citation_count=extra.get("citation_count", paper.citation_count),
            degree_type=degree_type,
        )

        cards[cn] = PaperCitationCard(
            key=cn,
            in_service=True,
            paper_id=cn,
            title=paper.title,
            title_en=paper.title_en,
            authors=list(paper.authors or []) or None,
            journal_name=paper.journal_name,
            pub_year=paper.pubyear,
            doi=paper.doi,
            abstract=paper.abstract,
            keywords=list(paper.keywords_ko or []) or None,
            paper_type=card_paper_type,
            kci_registered=paper.db_code == "JAKO",
            sci_indexed=bool(sci_indexed) if sci_indexed is not None else False,
            citation_count=paper.citation_count,
            trust_badge=trust_badge,
            is_bookmarked=False,
        )
    return cards


def _card_from_external(ref: PaperCitationExternalRef) -> PaperCitationCard:
    return PaperCitationCard(
        key=ref.external_id,
        in_service=False,
        paper_id=None,
        title=ref.title,
        title_en=None,
        authors=list(ref.authors) if ref.authors else None,
        journal_name=ref.journal,
        pub_year=ref.pubyear,
        doi=ref.doi,
        abstract=None,
        keywords=None,
        paper_type=None,
        kci_registered=None,
        sci_indexed=None,
        citation_count=None,
        trust_badge=None,
        is_bookmarked=None,
    )


async def _build_cards_for_nodes(db: AsyncSession, nodes: list[PaperCitationNode], external_refs_by_key: dict[str, PaperCitationExternalRef]) -> list[PaperCitationCard]:
    in_service_keys = [n.key for n in nodes if n.in_service]
    in_service_cards = await _build_in_service_cards(db, in_service_keys)

    cards: list[PaperCitationCard] = []
    for node in nodes:
        if node.in_service:
            card = in_service_cards.get(node.key)
        else:
            ref = external_refs_by_key.get(node.key)
            card = _card_from_external(ref) if ref else None
        if card:
            cards.append(card)
    return cards


# ---------------------------------------------------------------------------
# 최초 로드
# ---------------------------------------------------------------------------

async def get_citation_graph(cn: str, direction: Direction, db: AsyncSession) -> PaperCitationGraphResponse:
    """Neo4j 드라이버가 동기(sync)이므로 스레드풀에서 실행해 이벤트 루프 블로킹을 방지."""
    partial = await asyncio.to_thread(_get_or_build_in_service_part, cn, direction)
    if partial is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"paper not found: {cn}")

    nodes: list[PaperCitationNode] = [partial.center, *partial.nodes]
    edges: list[PaperCitationEdge] = list(partial.edges)

    # 코퍼스 안 논문이 external_refs에 섞여 들어온 행은 repository의 anti-join이 이미 걸러내지만,
    # 화면에 놓인 key를 한 번 더 넘겨 어떤 경우에도 같은 key가 두 노드로 생기지 않게 한다.
    placed_keys = [n.key for n in nodes]

    remaining = settings.paper_citation_summary_limit - len(partial.nodes)
    external_refs: list[PaperCitationExternalRef] = []
    if remaining > 0:
        external_refs = await paper_citation_repository.get_external_refs(
            db, cn, direction, limit=remaining, excluded_ids=placed_keys
        )
        for ref in external_refs:
            nodes.append(_node_from_external(ref, tier=1, side="child"))
            edges.append(_citation_edge_for(direction, cn, ref.external_id))

    excluded_ext_ids = placed_keys + [r.external_id for r in external_refs]
    has_more_external = (
        await paper_citation_repository.count_remaining_external_refs(db, cn, direction, excluded_ids=excluded_ext_ids)
    ) > 0
    has_more = partial.has_more_in_service or has_more_external

    external_refs_by_key = {r.external_id: r for r in external_refs}
    child_nodes = nodes[1:]  # center 제외
    papers = await _build_cards_for_nodes(db, child_nodes, external_refs_by_key)

    return PaperCitationGraphResponse(
        direction=direction,
        center=partial.center,
        nodes=nodes,
        edges=edges,
        has_more=has_more,
        papers=papers,
    )


# ---------------------------------------------------------------------------
# 노드 확장
# ---------------------------------------------------------------------------

@dataclass
class _InServiceExpandPartial:
    nodes: list[PaperCitationNode]
    edges: list[PaperCitationEdge]
    parent_has_more_in_service: bool


def _build_in_service_expand_sync(
    node_key: str, direction: Direction, excluded: list[str], fetch_limit: int, new_tier: int
) -> Optional[_InServiceExpandPartial]:
    driver = get_neo4j_driver()
    try:
        repo = GraphRepository(driver)
        node_dict = repo.find_paper(node_key)
        if node_dict is None:
            return None

        excluded_set = set(excluded)
        fresh = repo.find_citation_neighbors(node_key, direction=direction, limit=fetch_limit, excluded_cns=list(excluded_set))

        nodes = [_node_from_in_service(c, tier=new_tier, side="child") for c in fresh]
        edges = [_citation_edge_for(direction, node_key, c["cn"]) for c in fresh]

        fresh_cns = {c["cn"] for c in fresh}
        full_excluded = excluded_set | fresh_cns
        for node, c in zip(nodes, fresh):
            node.has_more = repo.has_more_citation_neighbors(c["cn"], direction=direction, excluded_cns=list(full_excluded))

        parent_has_more_in_service = repo.has_more_citation_neighbors(node_key, direction=direction, excluded_cns=list(full_excluded))

        return _InServiceExpandPartial(nodes=nodes, edges=edges, parent_has_more_in_service=parent_has_more_in_service)
    finally:
        driver.close()


async def expand_citation_node(
    node_key: str,
    *,
    direction: Direction,
    current_tier: int,
    existing_node_keys: list[str],
    db: AsyncSession,
) -> PaperCitationExpandResponse:
    excluded = set(existing_node_keys) | {node_key}
    remaining_capacity = settings.paper_citation_max_nodes - len(excluded)

    if remaining_capacity <= 0:
        return PaperCitationExpandResponse(
            parent_key=node_key, direction=direction, new_nodes=[], new_edges=[],
            parent_has_more=True, capped=True, papers=[],
        )

    fetch_limit = min(settings.paper_citation_expand_max, remaining_capacity)
    new_tier = current_tier + 1

    partial = await asyncio.to_thread(
        _build_in_service_expand_sync, node_key, direction, list(excluded), fetch_limit, new_tier
    )
    if partial is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"paper not found: {node_key}")

    nodes: list[PaperCitationNode] = list(partial.nodes)
    edges: list[PaperCitationEdge] = list(partial.edges)

    # excluded(기존 화면 노드)에 더해, 이번 확장으로 방금 붙은 in-service 노드도 제외해야
    # 같은 논문이 외부 노드로 한 번 더 들어오지 않는다.
    fresh_keys = [n.key for n in partial.nodes]
    ext_excluded = list(excluded) + fresh_keys

    remaining_after_in_service = min(settings.paper_citation_expand_max, remaining_capacity) - len(partial.nodes)
    external_refs: list[PaperCitationExternalRef] = []
    if remaining_after_in_service > 0:
        external_refs = await paper_citation_repository.get_external_refs(
            db, node_key, direction, limit=remaining_after_in_service, excluded_ids=ext_excluded
        )
        for ref in external_refs:
            nodes.append(_node_from_external(ref, tier=new_tier, side="child"))
            edges.append(_citation_edge_for(direction, node_key, ref.external_id))

    excluded_ext_ids = ext_excluded + [r.external_id for r in external_refs]
    has_more_external = (
        await paper_citation_repository.count_remaining_external_refs(db, node_key, direction, excluded_ids=excluded_ext_ids)
    ) > 0
    parent_has_more = partial.parent_has_more_in_service or has_more_external

    # 이번 확장으로 더 가져올 수 있었지만 100개 캡 때문에 못 가져온 경우만 capped=True
    capped = remaining_capacity < settings.paper_citation_expand_max and parent_has_more

    external_refs_by_key = {r.external_id: r for r in external_refs}
    papers = await _build_cards_for_nodes(db, nodes, external_refs_by_key)

    return PaperCitationExpandResponse(
        parent_key=node_key,
        direction=direction,
        new_nodes=nodes,
        new_edges=edges,
        parent_has_more=parent_has_more,
        capped=capped,
        papers=papers,
    )
