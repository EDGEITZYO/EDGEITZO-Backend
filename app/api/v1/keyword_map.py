from __future__ import annotations

import json
import uuid as _uuid
from typing import List, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.response import success_response
from app.models.user_keyword_map import UserKeywordMap
from app.services.keyword_map_service import expand_keyword_node, generate_keyword_map

_AXIS_LABEL = {
    "core_technology": "핵심기술",
    "research_target": "연구대상",
    "parent_domain": "상위분야",
    "application_domain": "응용분야",
}


def _transform_tree(raw: dict) -> dict:
    """LLM 4축 트리 → {root > children(recursive, edge_type 포함)} 변환."""
    root_raw = raw.get("root", {})
    axes = raw.get("axes", {})

    def _convert_node(node: dict, edge_type: str | None, depth: int) -> dict:
        node_id = f"{node.get('en', node.get('ko', ''))}-{depth}".replace(" ", "_").lower()
        children_raw = node.get("children", [])
        return {
            "id": node_id,
            "ko": node.get("ko", ""),
            "en": node.get("en", ""),
            "depth": depth,
            "edge_type": edge_type,
            "definition": node.get("definition"),
            "children": [_convert_node(c, edge_type, depth + 1) for c in children_raw],
        }

    children: list[dict] = []
    for axis_key, axis_label in _AXIS_LABEL.items():
        for node in axes.get(axis_key, []):
            children.append(_convert_node(node, axis_label, 1))

    return {
        "id": "root",
        "ko": root_raw.get("ko", ""),
        "en": root_raw.get("en", ""),
        "depth": 0,
        "edge_type": None,
        "definition": None,
        "children": children,
    }

router = APIRouter()

_REDIS_DB = 7
_MAP_TTL = 86400  # 24시간


class KeywordMapRequest(BaseModel):
    research_field: str
    user_id: str = ""  # 생성 결과를 캐시에 저장할 때 사용


class KeywordMapGenerateResponse(BaseModel):
    research_field: str
    tree: dict
    # Neo4j 적재는 B파트 담당 — 여기서는 생성만


@router.get(
    "/keyword-map",
    summary="키워드맵 트리 조회",
    description="topic으로 LLM 키워드 트리 생성. root > children 재귀 구조, edge_type(핵심기술/연구대상/상위분야/응용분야) 포함.",
)
async def get_keyword_map_by_topic(
    topic: str = Query(..., description="연구 분야 (예: 유전자 발현)"),
    user_id: str = Query(default="", description="캐시 저장용 user_id (선택)"),
    db: AsyncSession = Depends(get_db),
):
    # Redis 캐시 확인
    r = get_redis(_REDIS_DB)
    cache_key = f"keyword_map_tree:{topic}"
    cached = r.get(cache_key)
    if cached:
        return success_response(
            data={"topic": topic, "root": json.loads(cached)},
            message="keyword map (cached)",
        )

    raw = await generate_keyword_map(topic)
    tree = _transform_tree(raw)
    r.set(cache_key, json.dumps(tree, ensure_ascii=False), ex=_MAP_TTL)

    # user_id 있으면 기존 사용자 맵 저장도 유지
    if user_id:
        r.set(
            f"keyword_map:{user_id}",
            json.dumps({"research_field": topic, "tree": raw}, ensure_ascii=False),
            ex=_MAP_TTL,
        )

    return success_response(
        data={"topic": topic, "root": tree},
        message="keyword map generated",
    )


@router.get(
    "/keyword-map/node/{node_id}/papers",
    summary="키워드 노드 논문 목록",
    description="node_id(키워드명)로 Neo4j 조회 → ChromaDB에서 메타데이터 fetch. 필터: year/type/sci/kci/page.",
)
async def get_node_papers(
    node_id: str,
    year: Optional[str] = Query(None, description="'3y'|'5y'|'10y'|'all'"),
    type: Optional[str] = Query(None, description="'저널'|'학위논문'|'학회'"),
    sci: Optional[bool] = Query(None, description="SCI 등재 여부"),
    kci: Optional[bool] = Query(None, description="KCI 등재 여부"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
):
    from app.api.v1.keyword_search import (
        KeywordPaperRequest,
        _apply_year_filter,
        _sort_items,
        _to_result,
    )
    from app.services.chroma_search_service import get_chroma_search_service
    from app.services.neo4j_search_service import get_paper_ids_by_keyword

    service = get_chroma_search_service()
    paper_ids = await get_paper_ids_by_keyword(node_id)

    if paper_ids:
        items = await service.get_items_by_ids(paper_ids)
    else:
        items = await service.search(query=node_id, n_results=size * 2)

    items = _apply_year_filter(items, year)

    # type 필터
    _tmap = {"JAKO": "저널", "JAFO": "저널", "DIKO": "학위논문", "CFKO": "학회"}
    if type:
        items = [i for i in items if _tmap.get(i.db_code or "") == type]
    # kci 필터
    if kci is True:
        items = [i for i in items if i.db_code == "JAKO"]
    elif kci is False:
        items = [i for i in items if i.db_code != "JAKO"]
    # sci 필터 (현재 데이터에 SCI 없으므로 kci=False와 동일 효과)
    if sci is True:
        items = [i for i in items if i.db_code in ("SCIE", "SSCI", "AHCI")]

    items = _sort_items(items, "date")
    total = len(items)
    offset = (page - 1) * size
    paged = items[offset: offset + size]

    return success_response(
        data={
            "keyword": node_id,
            "total": total,
            "page": page,
            "papers": [_to_result(p) for p in paged],
        },
        message="node papers fetched",
    )


@router.post("/keyword-map/generate")
async def generate_map(request: KeywordMapRequest, db: AsyncSession = Depends(get_db)):
    """연구분야 텍스트 → 4축 키워드 트리 JSON 생성"""
    tree = await generate_keyword_map(request.research_field)

    if request.user_id:
        # Redis는 문자열 user_id 그대로 저장 (기존 동작 유지)
        r = get_redis(_REDIS_DB)
        r.set(
            f"keyword_map:{request.user_id}",
            json.dumps({"research_field": request.research_field, "tree": tree}, ensure_ascii=False),
            ex=_MAP_TTL,
        )

        # DB는 user_id가 유효한 UUID일 때만 저장 — 비정상 값이면 generate가 죽지 않도록
        try:
            uid = _uuid.UUID(request.user_id)
        except ValueError:
            uid = None

        if uid is not None:
            stmt = pg_insert(UserKeywordMap).values(
                id=_uuid.uuid4(),
                user_id=uid,
                research_field=request.research_field,
                tree=tree,
            ).on_conflict_do_update(
                constraint="uq_user_keyword_maps_user_id",
                set_={
                    "research_field": request.research_field,
                    "tree": tree,
                    "updated_at": sa.func.now(),
                },
            )
            await db.execute(stmt)
            await db.commit()

    return success_response(
        data=KeywordMapGenerateResponse(
            research_field=request.research_field,
            tree=tree,
        ),
        message="keyword map generated",
    )


class ExpandNodeRequest(BaseModel):
    parent_label: str       # 부모 노드 한글명
    parent_label_en: str = ""
    axis: str               # "핵심기술" | "연구대상" | "상위분야" | "응용분야"
    research_field: str     # 최상위 연구분야 (컨텍스트용)
    depth: int = 1          # 부모 노드의 현재 depth


class ExpandNodeResponse(BaseModel):
    parent_label: str
    new_children: list[dict]


@router.post(
    "/keyword-map/node/{node_id}/expand",
    summary="키워드 노드 하위 키워드 LLM 생성",
    description="선택된 노드(node_id)의 하위 키워드 2~3개를 LLM으로 생성. axis, research_field, depth 정보 필수.",
)
async def expand_node(
    node_id: str,
    request: ExpandNodeRequest,
):
    # node_id가 부모 노드명 (URL 경로), request.parent_label은 오버라이드 용
    parent_label = request.parent_label or node_id
    children = await expand_keyword_node(
        parent_label=parent_label,
        parent_label_en=request.parent_label_en or "",
        axis=request.axis,
        research_field=request.research_field,
        depth=request.depth,
    )
    return success_response(
        data=ExpandNodeResponse(
            parent_label=parent_label,
            new_children=children,
        ),
        message="node expanded",
    )
