from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Literal, Optional

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.api.v1.home import save_search_history
from app.core.neo4j_client import get_neo4j_driver
from app.core.redis import get_redis
from app.core.settings import settings
from app.repositories.graph_repository import GraphRepository
from app.schemas.keyword_map import KeywordMapEdge, KeywordMapExpandResponse, KeywordMapGraphResponse, KeywordMapNode
from app.schemas.paper import PaperListResponse
from app.services.chroma_search_service import get_chroma_search_service
from app.services.keywords.keyword_db import search_keywords
from app.services.neo4j_search_service import get_paper_ids_by_keyword
from app.services.paper_filter_service import (
    apply_filters,
    apply_paper_type_postfilter,
    apply_sort,
    build_paper_cards,
)

logger = logging.getLogger(__name__)

_REDIS_DB = 7


@dataclass
class _Candidate:
    key: str
    name_ko: str | None
    name_en: str | None
    own_paper_count: int
    cooccurrence_paper_count: int


@dataclass
class _AnchorInfo:
    key: str
    name_ko: str | None
    name_en: str | None
    paper_count: int


@dataclass
class _SubgraphResult:
    anchor: KeywordMapNode
    nodes: list[KeywordMapNode]
    edges: list[KeywordMapEdge]
    has_more_children: bool
    placed_keys: set[str]


def _split_name(d: dict) -> tuple[str | None, str | None]:
    name = d.get("name")
    return (name, None) if d.get("lang") == "ko" else (None, name)


def _anchor_from_repo_dict(d: dict) -> _AnchorInfo:
    name_ko, name_en = _split_name(d)
    return _AnchorInfo(key=d["key"], name_ko=name_ko, name_en=name_en, paper_count=d.get("paper_count", 0))


def _candidate_from_related_item(item: dict) -> _Candidate:
    node, edge = item["node"], item["edge"]
    name_ko, name_en = _split_name(node)
    return _Candidate(
        key=node["key"],
        name_ko=name_ko,
        name_en=name_en,
        own_paper_count=node.get("paper_count", 0),
        cooccurrence_paper_count=edge.get("paper_count", 0),
    )


def _node_from_candidate(c: _Candidate, *, tier: int, has_more: bool = False) -> KeywordMapNode:
    return KeywordMapNode(key=c.key, name_ko=c.name_ko, name_en=c.name_en, tier=tier, side="child", paper_count=c.own_paper_count, has_more=has_more)


def _fetch_candidates(repo: GraphRepository, node_key: str) -> list[_Candidate]:
    raw = repo.find_related_keywords(node_key, limit=settings.keyword_map_candidate_pool_size, min_paper_count=1)
    return [_candidate_from_related_item(item) for item in raw]


def _classify(candidates: list[_Candidate], node_own_paper_count: int) -> list[_Candidate]:
    """빈도 <= 기준인 후보만 하위로 채택(동률 포함, 확정된 규칙). 동시출현 수(유사도) 내림차순 정렬."""
    return sorted(
        (c for c in candidates if c.own_paper_count <= node_own_paper_count),
        key=lambda c: c.cooccurrence_paper_count,
        reverse=True,
    )


def _has_more_children(candidates: list[_Candidate], node_own_paper_count: int) -> bool:
    # _classify의 동률 포함 규칙과 달리 여기는 엄격한 부등호만 인정 (동률만 있는 경우는 "더 이상 하위 없음"으로 취급)
    return any(c.own_paper_count < node_own_paper_count for c in candidates)


def _compute_has_more(repo: GraphRepository, key: str, own_paper_count: int, excluded: set[str]) -> bool:
    """이 노드를 expand했을 때 excluded(이미 화면에 있는 노드)에 안 걸리는 자식이 하나라도 남아있는지."""
    candidates = _fetch_candidates(repo, key)
    children = _classify(candidates, own_paper_count)
    return any(c.key not in excluded for c in children)


def _attach_cross_links(repo: GraphRepository, nodes: list[KeywordMapNode], edges: list[KeywordMapEdge], placed_keys: set[str]) -> None:
    if len(placed_keys) < 2:
        return
    tree_pairs = {frozenset((e.source, e.target)) for e in edges}
    relations = repo.find_relations_among(list(placed_keys), min_paper_count=1)

    cross_link_degree: dict[str, int] = {}
    for r in relations:
        pair = frozenset((r["source"], r["target"]))
        if pair in tree_pairs:
            continue
        edges.append(KeywordMapEdge(source=r["source"], target=r["target"], type="cross_link", paper_count=r["paper_count"]))
        cross_link_degree[r["source"]] = cross_link_degree.get(r["source"], 0) + 1
        cross_link_degree[r["target"]] = cross_link_degree.get(r["target"], 0) + 1

    for n in nodes:
        n.cross_link_count = cross_link_degree.get(n.key, 0)
        n.is_hub = n.cross_link_count >= settings.keyword_map_hub_cross_link_threshold


def _build_anchor_subgraph(repo: GraphRepository, anchor: _AnchorInfo) -> _SubgraphResult:
    anchor_node = KeywordMapNode(key=anchor.key, name_ko=anchor.name_ko, name_en=anchor.name_en, tier=0, side="anchor", paper_count=anchor.paper_count)
    placed_keys: set[str] = {anchor.key}
    nodes: list[KeywordMapNode] = [anchor_node]
    edges: list[KeywordMapEdge] = []

    candidates0 = _fetch_candidates(repo, anchor.key)
    children0 = _classify(candidates0, anchor.paper_count)
    has_more_children = _has_more_children(candidates0, anchor.paper_count)

    tier1_pairs: list[tuple[KeywordMapNode, _Candidate]] = []

    l1_candidates = [c for c in children0 if c.key not in placed_keys][: settings.keyword_map_child_l1_max]
    for c in l1_candidates:
        placed_keys.add(c.key)
        node = _node_from_candidate(c, tier=1)
        nodes.append(node)
        tier1_pairs.append((node, c))
        edges.append(KeywordMapEdge(source=anchor.key, target=c.key, type="tree", paper_count=c.cooccurrence_paper_count))

    # tier-2는 여기서 미리 채우지 않음 — 사용자가 expand를 눌렀을 때만 계산(expand_node).
    # 대신 각 tier-1 노드가 "펼치면 뭐가 나오긴 하는지"만 미리 확인해서 has_more로 알려준다.
    for node, candidate in tier1_pairs:
        node.has_more = _compute_has_more(repo, candidate.key, candidate.own_paper_count, placed_keys)

    _attach_cross_links(repo, nodes, edges, placed_keys)

    return _SubgraphResult(
        anchor=anchor_node,
        nodes=nodes,
        edges=edges,
        has_more_children=has_more_children,
        placed_keys=placed_keys,
    )


def _cache_key_for_anchor(anchor_key: str) -> str:
    return f"keyword_map:subgraph:{anchor_key}"


def _serialize_subgraph(result: _SubgraphResult) -> str:
    return json.dumps(
        {
            "anchor": result.anchor.model_dump(),
            "nodes": [n.model_dump() for n in result.nodes],
            "edges": [e.model_dump() for e in result.edges],
            "has_more_children": result.has_more_children,
        },
        ensure_ascii=False,
    )


def _deserialize_subgraph(raw: str) -> _SubgraphResult:
    data = json.loads(raw)
    nodes = [KeywordMapNode(**n) for n in data["nodes"]]
    return _SubgraphResult(
        anchor=KeywordMapNode(**data["anchor"]),
        nodes=nodes,
        edges=[KeywordMapEdge(**e) for e in data["edges"]],
        has_more_children=data["has_more_children"],
        placed_keys={n.key for n in nodes},
    )


def _get_or_build_anchor_subgraph(repo: GraphRepository, anchor: _AnchorInfo) -> _SubgraphResult:
    """앵커 key만으로 결정되는 순수 Neo4j 계산 결과를 캐시. client별 상태(existing_node_keys 등)는 여기서 다루지 않음
    — 짧은 TTL의 성능 캐시일 뿐 source of truth가 아니므로, Neo4j 데이터가 바뀌면 TTL 내에 자연히 갱신됨."""
    cache_key = _cache_key_for_anchor(anchor.key)
    try:
        r = get_redis(_REDIS_DB)
        cached = r.get(cache_key)
        if cached:
            return _deserialize_subgraph(cached)
    except Exception:
        logger.warning("키워드맵 서브그래프 캐시 조회 실패", exc_info=True)

    result = _build_anchor_subgraph(repo, anchor)

    try:
        r = get_redis(_REDIS_DB)
        r.set(cache_key, _serialize_subgraph(result), ex=settings.keyword_map_cache_ttl_seconds)
    except Exception:
        logger.warning("키워드맵 서브그래프 캐시 저장 실패", exc_info=True)

    return result


def _get_initial_anchor_map_sync(keyword_text: str) -> KeywordMapGraphResponse:
    matches = search_keywords(keyword_text, limit=1)
    if not matches:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"keyword not found: {keyword_text}")
    kw = matches[0]
    anchor = _AnchorInfo(key=kw.key, name_ko=kw.name_ko, name_en=kw.name_en, paper_count=kw.paper_count)

    driver = get_neo4j_driver()
    try:
        repo = GraphRepository(driver)
        result = _get_or_build_anchor_subgraph(repo, anchor)
    finally:
        driver.close()

    return KeywordMapGraphResponse(
        anchor=result.anchor,
        nodes=result.nodes,
        edges=result.edges,
        has_more_children=result.has_more_children,
    )


async def get_initial_anchor_map(keyword_text: str) -> KeywordMapGraphResponse:
    """Neo4j 드라이버가 동기(sync)이므로 스레드풀에서 실행해 이벤트 루프 블로킹을 방지 (get_paper_ids_by_keyword와 동일 패턴)."""
    return await asyncio.to_thread(_get_initial_anchor_map_sync, keyword_text)


def _recenter_keyword_map_sync(new_anchor_key: str, existing_node_keys: list[str]) -> KeywordMapGraphResponse:
    driver = get_neo4j_driver()
    try:
        repo = GraphRepository(driver)
        anchor_dict = repo.find_keyword(new_anchor_key)
        if anchor_dict is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"keyword not found: {new_anchor_key}")
        anchor = _anchor_from_repo_dict(anchor_dict)
        result = _get_or_build_anchor_subgraph(repo, anchor)

        nodes = list(result.nodes)
        edges = list(result.edges)

        # carry-forward: 새 그래프에 없는 이전 화면 노드 중, 새 그래프와 실제로 연결된 것만 유지 (연결 안 되면 응답에서 자연히 빠짐)
        carried_paper_count: dict[str, int] = {}
        carry_keys = [k for k in existing_node_keys if k not in result.placed_keys and k != anchor.key]
        if carry_keys:
            carried = [(k, d) for k in carry_keys if (d := repo.find_keyword(k)) is not None]
            if carried:
                probe_keys = list(result.placed_keys) + [k for k, _ in carried]
                relations = repo.find_relations_among(probe_keys, min_paper_count=1)
                connected = {r["source"] for r in relations} | {r["target"] for r in relations}
                tree_pairs = {frozenset((e.source, e.target)) for e in edges}

                child_count = sum(1 for n in nodes if n.tier == 1 and n.side == "child")
                # carry-forward 노드는 항상 cross-link(점선)로만 연결 — 새로 계산된 tree가 아니므로
                # "정확한 부모가 어디인지"는 애매함. tier는 좌우 배치용으로만 부여.
                # anchor보다 빈도 높은(상위) 후보는 parent 개념 제거로 더 이상 표시하지 않으므로 그냥 드롭.
                for k, d in carried:
                    if k not in connected:
                        continue
                    if d.get("paper_count", 0) > anchor.paper_count:
                        continue
                    if child_count >= settings.keyword_map_child_l1_max:
                        continue
                    name_ko, name_en = _split_name(d)
                    nodes.append(KeywordMapNode(key=k, name_ko=name_ko, name_en=name_en, tier=1, side="child", paper_count=d.get("paper_count", 0)))
                    child_count += 1
                    carried_paper_count[k] = d.get("paper_count", 0)

                final_keys = {n.key for n in nodes}
                for r in relations:
                    pair = frozenset((r["source"], r["target"]))
                    if pair in tree_pairs or r["source"] not in final_keys or r["target"] not in final_keys:
                        continue
                    edges.append(KeywordMapEdge(source=r["source"], target=r["target"], type="cross_link", paper_count=r["paper_count"]))
                    tree_pairs.add(pair)

        if len(nodes) > settings.keyword_map_max_nodes:
            core_keys = result.placed_keys
            carry_nodes = [n for n in nodes if n.key not in core_keys]
            overflow = len(nodes) - settings.keyword_map_max_nodes
            drop = {n.key for n in carry_nodes[-overflow:]}
            nodes = [n for n in nodes if n.key not in drop]
            edges = [e for e in edges if e.source not in drop and e.target not in drop]

        final_keys = {n.key for n in nodes}

        if carried_paper_count:
            visible = set(result.placed_keys) | final_keys
            for n in nodes:
                if n.key in carried_paper_count:
                    n.has_more = _compute_has_more(repo, n.key, carried_paper_count[n.key], visible)

        cross_link_degree: dict[str, int] = {}
        for e in edges:
            if e.type != "cross_link":
                continue
            if e.source in final_keys:
                cross_link_degree[e.source] = cross_link_degree.get(e.source, 0) + 1
            if e.target in final_keys:
                cross_link_degree[e.target] = cross_link_degree.get(e.target, 0) + 1
        for n in nodes:
            n.cross_link_count = cross_link_degree.get(n.key, 0)
            n.is_hub = n.cross_link_count >= settings.keyword_map_hub_cross_link_threshold
    finally:
        driver.close()

    return KeywordMapGraphResponse(
        anchor=result.anchor,
        nodes=nodes,
        edges=edges,
        has_more_children=result.has_more_children,
    )


async def recenter_keyword_map(new_anchor_key: str, existing_node_keys: list[str]) -> KeywordMapGraphResponse:
    """Neo4j 드라이버가 동기(sync)이므로 스레드풀에서 실행해 이벤트 루프 블로킹을 방지."""
    return await asyncio.to_thread(_recenter_keyword_map_sync, new_anchor_key, existing_node_keys)


def _expand_node_sync(node_key: str, *, current_tier: int, existing_node_keys: list[str]) -> KeywordMapExpandResponse:
    driver = get_neo4j_driver()
    try:
        repo = GraphRepository(driver)
        node_dict = repo.find_keyword(node_key)
        if node_dict is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"keyword not found: {node_key}")

        candidates = _fetch_candidates(repo, node_key)
        children = _classify(candidates, node_dict.get("paper_count", 0))

        excluded = set(existing_node_keys) | {node_key}
        remaining = [c for c in children if c.key not in excluded]
        take = remaining[: settings.keyword_map_expand_max]
        parent_has_more = len(remaining) > len(take)

        new_tier = current_tier + 1
        new_nodes = [_node_from_candidate(c, tier=new_tier) for c in take]
        new_edges = [
            KeywordMapEdge(source=node_key, target=c.key, type="tree", paper_count=c.cooccurrence_paper_count) for c in take
        ]

        if new_nodes:
            new_keys = {c.key for c in take}
            full_excluded = excluded | new_keys
            for node, c in zip(new_nodes, take):
                node.has_more = _compute_has_more(repo, c.key, c.own_paper_count, full_excluded)

            probe_keys = list(full_excluded)
            relations = repo.find_relations_among(probe_keys, min_paper_count=1)
            tree_pairs = {frozenset((e.source, e.target)) for e in new_edges}
            for r in relations:
                pair = frozenset((r["source"], r["target"]))
                if pair in tree_pairs:
                    continue
                # 신규 노드가 하나라도 관련된 관계만 cross-link로 노출 (기존 노드끼리의 관계는 이미 화면에 반영돼 있음)
                if r["source"] not in new_keys and r["target"] not in new_keys:
                    continue
                new_edges.append(KeywordMapEdge(source=r["source"], target=r["target"], type="cross_link", paper_count=r["paper_count"]))
    finally:
        driver.close()

    return KeywordMapExpandResponse(parent_key=node_key, new_nodes=new_nodes, new_edges=new_edges, parent_has_more=parent_has_more)


async def expand_node(node_key: str, *, current_tier: int, existing_node_keys: list[str]) -> KeywordMapExpandResponse:
    """Neo4j 드라이버가 동기(sync)이므로 스레드풀에서 실행해 이벤트 루프 블로킹을 방지."""
    return await asyncio.to_thread(
        _expand_node_sync, node_key, current_tier=current_tier, existing_node_keys=existing_node_keys
    )


def _get_keyword_names_sync(node_key: str) -> Optional[tuple[str | None, str | None]]:
    driver = get_neo4j_driver()
    try:
        repo = GraphRepository(driver)
        d = repo.find_keyword(node_key)
    finally:
        driver.close()
    if d is None:
        return None
    return _split_name(d)


async def get_keyword_names(node_key: str) -> Optional[tuple[str | None, str | None]]:
    """상세 패널(정의 조회)에서 node_key -> (name_ko, name_en) 해석용. 없으면 None (404 처리).
    Neo4j 드라이버가 동기(sync)이므로 스레드풀에서 실행해 이벤트 루프 블로킹을 방지."""
    return await asyncio.to_thread(_get_keyword_names_sync, node_key)


async def get_node_papers(
    *,
    keyword: str,
    keyword_en: str = "",
    year: Optional[int] = None,
    paper_type: Optional[str] = None,
    kci: Optional[bool] = None,
    sci: Optional[bool] = None,
    sort: Literal["relevance", "latest", "oldest", "citation"] = "relevance",
    user_id: Optional[str] = None,
    keyword_path: Optional[str] = None,
    map_session_id: Optional[str] = None,
    research_field: Optional[str] = None,
    db: AsyncSession,
) -> PaperListResponse:
    """키워드 노드 클릭 시 논문 리스트 조회 — keyword_map.py(GET, path param)와
    keyword_search.py(POST, body) 양쪽에서 공용으로 호출 (기존에 두 파일에 따로 구현돼 있던 로직 통합).
    페이지네이션 없이 필터링된 전체 결과를 한 번에 반환한다."""
    service = get_chroma_search_service()
    paper_ids = await get_paper_ids_by_keyword(keyword)

    if paper_ids:
        items = await service.get_items_by_ids(paper_ids)
    else:
        query = f"{keyword} {keyword_en}".strip()
        items = await service.search(query=query)

    items = apply_filters(items, year=year, paper_type=paper_type, kci=kci, sci=sci)
    items = apply_sort(items, sort)

    all_cards = await build_paper_cards(items, db, user_id=user_id)
    all_cards = apply_paper_type_postfilter(all_cards, paper_type)

    saved_search_id = None
    if user_id:
        try:
            saved_search_id = map_session_id or str(uuid.uuid4())
            save_search_history(
                user_id=user_id,
                search_type="keyword",
                title=research_field or keyword,
                search_id=saved_search_id,
                keyword_path=[k.strip() for k in keyword_path.split(",")] if keyword_path else [keyword],
                map_session_id=map_session_id,
            )
        except Exception:
            saved_search_id = None

    return PaperListResponse(keyword=keyword, papers=all_cards, total=len(all_cards), search_id=saved_search_id)
