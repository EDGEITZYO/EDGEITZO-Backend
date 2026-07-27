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
    paginate,
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
    has_more_parents: bool
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


def _node_from_candidate(c: _Candidate, *, tier: int, side: Literal["parent", "child"]) -> KeywordMapNode:
    return KeywordMapNode(key=c.key, name_ko=c.name_ko, name_en=c.name_en, tier=tier, side=side, paper_count=c.own_paper_count)


def _fetch_candidates(repo: GraphRepository, node_key: str) -> list[_Candidate]:
    raw = repo.find_related_keywords(node_key, limit=settings.keyword_map_candidate_pool_size, min_paper_count=1)
    return [_candidate_from_related_item(item) for item in raw]


def _classify(candidates: list[_Candidate], node_own_paper_count: int) -> tuple[list[_Candidate], list[_Candidate]]:
    """빈도 > 기준 -> 상위, <= 기준 -> 하위(동률은 하위 취급, 확정된 규칙). 각 그룹은 동시출현 수(유사도) 내림차순."""
    parents = sorted(
        (c for c in candidates if c.own_paper_count > node_own_paper_count),
        key=lambda c: c.cooccurrence_paper_count,
        reverse=True,
    )
    children = sorted(
        (c for c in candidates if c.own_paper_count <= node_own_paper_count),
        key=lambda c: c.cooccurrence_paper_count,
        reverse=True,
    )
    return parents, children


def _has_more_parents(candidates: list[_Candidate], node_own_paper_count: int) -> bool:
    return any(c.own_paper_count > node_own_paper_count for c in candidates)


def _has_more_children(candidates: list[_Candidate], node_own_paper_count: int) -> bool:
    # _classify의 동률->하위 규칙과 달리 여기는 엄격한 부등호만 인정 (동률만 있는 경우는 "더 이상 하위 없음"으로 취급)
    return any(c.own_paper_count < node_own_paper_count for c in candidates)


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
    parents0, children0 = _classify(candidates0, anchor.paper_count)
    has_more_parents = _has_more_parents(candidates0, anchor.paper_count)
    has_more_children = _has_more_children(candidates0, anchor.paper_count)

    parent_nodes = [c for c in parents0 if c.key not in placed_keys][: settings.keyword_map_parent_max]
    for p in parent_nodes:
        placed_keys.add(p.key)
        nodes.append(_node_from_candidate(p, tier=1, side="parent"))
        edges.append(KeywordMapEdge(source=p.key, target=anchor.key, type="tree", paper_count=p.cooccurrence_paper_count))

    l1_candidates = [c for c in children0 if c.key not in placed_keys][: settings.keyword_map_child_l1_max]
    for c in l1_candidates:
        placed_keys.add(c.key)
        nodes.append(_node_from_candidate(c, tier=1, side="child"))
        edges.append(KeywordMapEdge(source=anchor.key, target=c.key, type="tree", paper_count=c.cooccurrence_paper_count))

    # 2단계 자동 펼침: 1단계를 앵커와의 동시출현 랭킹 순으로 그리디 채택 — 소프트 타겟/하드캡을 넘기는 지점에서 즉시 중단
    # (스킵하고 다음 1단계로 넘어가지 않음 — "상위 몇 개" 같은 고정 매직넘버 대신 예산 기반으로 결정)
    running_total = len(nodes)
    budget = min(settings.keyword_map_initial_node_target, settings.keyword_map_max_nodes)
    for l1 in l1_candidates:
        candidates1 = _fetch_candidates(repo, l1.key)
        _, children1 = _classify(candidates1, l1.own_paper_count)
        take = [c for c in children1 if c.key not in placed_keys][: settings.keyword_map_child_l2_max_per_parent]
        if not take:
            continue
        if running_total + len(take) > budget:
            break
        for c in take:
            placed_keys.add(c.key)
            nodes.append(_node_from_candidate(c, tier=2, side="child"))
            edges.append(KeywordMapEdge(source=l1.key, target=c.key, type="tree", paper_count=c.cooccurrence_paper_count))
        running_total += len(take)

    _attach_cross_links(repo, nodes, edges, placed_keys)

    return _SubgraphResult(
        anchor=anchor_node,
        nodes=nodes,
        edges=edges,
        has_more_parents=has_more_parents,
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
            "has_more_parents": result.has_more_parents,
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
        has_more_parents=data["has_more_parents"],
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
        has_more_parents=result.has_more_parents,
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
        carry_keys = [k for k in existing_node_keys if k not in result.placed_keys and k != anchor.key]
        if carry_keys:
            carried = [(k, d) for k in carry_keys if (d := repo.find_keyword(k)) is not None]
            if carried:
                probe_keys = list(result.placed_keys) + [k for k, _ in carried]
                relations = repo.find_relations_among(probe_keys, min_paper_count=1)
                connected = {r["source"] for r in relations} | {r["target"] for r in relations}
                tree_pairs = {frozenset((e.source, e.target)) for e in edges}

                sibling_counts = {
                    "parent": sum(1 for n in nodes if n.tier == 1 and n.side == "parent"),
                    "child": sum(1 for n in nodes if n.tier == 1 and n.side == "child"),
                }
                # carry-forward 노드는 항상 cross-link(점선)로만 연결 — 새로 계산된 tree가 아니므로
                # "정확한 부모가 어디인지"는 애매함. tier/side는 좌우 배치용으로만 부여.
                for k, d in carried:
                    if k not in connected:
                        continue
                    side = "parent" if d.get("paper_count", 0) > anchor.paper_count else "child"
                    cap = settings.keyword_map_parent_max if side == "parent" else settings.keyword_map_child_l1_max
                    if sibling_counts[side] >= cap:
                        continue
                    name_ko, name_en = _split_name(d)
                    nodes.append(KeywordMapNode(key=k, name_ko=name_ko, name_en=name_en, tier=1, side=side, paper_count=d.get("paper_count", 0)))
                    sibling_counts[side] += 1

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
        has_more_parents=result.has_more_parents,
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
        _, children = _classify(candidates, node_dict.get("paper_count", 0))

        excluded = set(existing_node_keys) | {node_key}
        take = [c for c in children if c.key not in excluded][: settings.keyword_map_expand_max]

        new_tier = current_tier + 1
        new_nodes = [_node_from_candidate(c, tier=new_tier, side="child") for c in take]
        new_edges = [
            KeywordMapEdge(source=node_key, target=c.key, type="tree", paper_count=c.cooccurrence_paper_count) for c in take
        ]

        if new_nodes:
            new_keys = {c.key for c in take}
            probe_keys = list(excluded | new_keys)
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

    return KeywordMapExpandResponse(parent_key=node_key, new_nodes=new_nodes, new_edges=new_edges)


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
    year_range: Optional[str] = None,
    paper_type: Optional[str] = None,
    kci: Optional[bool] = None,
    sci: Optional[bool] = None,
    sort: Literal["citation", "date"] = "date",
    page: int = 1,
    size: int = 20,
    user_id: Optional[str] = None,
    keyword_path: Optional[str] = None,
    map_session_id: Optional[str] = None,
    research_field: Optional[str] = None,
    db: AsyncSession,
) -> PaperListResponse:
    """키워드 노드 클릭 시 논문 리스트 조회 — keyword_map.py(GET, path param)와
    keyword_search.py(POST, body) 양쪽에서 공용으로 호출 (기존에 두 파일에 따로 구현돼 있던 로직 통합)."""
    service = get_chroma_search_service()
    paper_ids = await get_paper_ids_by_keyword(keyword)

    if paper_ids:
        items = await service.get_items_by_ids(paper_ids)
    else:
        query = f"{keyword} {keyword_en}".strip()
        items = await service.search(query=query, n_results=size * 2)

    items = apply_filters(items, year_range=year_range, paper_type=paper_type, kci=kci, sci=sci)
    items = apply_sort(items, sort)

    all_cards = await build_paper_cards(items, db, user_id=user_id)
    all_cards = apply_paper_type_postfilter(all_cards, paper_type)
    paged, total = paginate(all_cards, page, size)

    saved_search_id = None
    if user_id and page == 1:
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

    return PaperListResponse(keyword=keyword, papers=paged, total=total, page=page, size=size, search_id=saved_search_id)
