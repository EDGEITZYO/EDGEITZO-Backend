from __future__ import annotations

import json
import uuid as _uuid
from typing import List, Literal, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.response import success_response
from app.models.user_keyword_map import UserKeywordMap
from app.schemas.common import ApiResponse
from app.schemas.paper import PaperListResponse
from app.services.chroma_search_service import get_chroma_search_service
from app.services.keyword_map_service import expand_keyword_node, generate_keyword_map
from app.services.neo4j_search_service import get_paper_ids_by_keyword
from app.services.paper_filter_service import apply_filters, apply_sort, build_paper_cards, paginate

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
    research_field: str = Field(description="키워드 트리를 생성할 연구 분야", example="딥러닝")
    user_id: str = Field("", description="생성 결과를 Redis/DB에 저장할 유저 ID (선택). 유효한 UUID일 때만 DB 저장")


class KeywordMapGenerateResponse(BaseModel):
    research_field: str = Field(description="생성 기준 연구 분야")
    tree: dict = Field(description="4축 키워드 트리 원본 JSON (root, axes 구조)")


@router.get(
    "/keyword-map",
    response_model=ApiResponse[dict],
    summary="키워드맵 트리 조회",
    description="""연구 분야(`topic`)로 LLM 키워드 트리를 생성해 반환합니다.

- Redis 캐시 우선 조회 (TTL 24시간), 미스 시 LLM 호출
- `user_id` 제공 시 개인 키워드맵도 Redis에 저장 (`/keyword-search/map/{user_id}`에서 조회 가능)

**응답 구조 (`data.root`)**
- `id`: 노드 고유 ID
- `ko` / `en`: 노드명 한/영
- `depth`: 트리 깊이 (root=0)
- `edge_type`: 축 분류. `'핵심기술'` | `'연구대상'` | `'상위분야'` | `'응용분야'` | null(root)
- `definition`: 노드 정의 (선택)
- `children`: 하위 노드 배열 (재귀 동일 구조)
""",
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
    response_model=ApiResponse[PaperListResponse],
    response_description="필터/정렬/페이지네이션 적용된 논문 카드 목록. trust_badge는 papers 테이블 존재 시 채워짐",
    responses={
        200: {"description": "정상 응답. papers 빈 배열도 200 반환"},
    },
    summary="키워드 노드 논문 목록",
    description="""노드명(`node_id`)으로 Neo4j 조회 후 ChromaDB에서 논문 메타데이터를 반환합니다. Neo4j 실패 시 ChromaDB 직접 검색으로 자동 fallback.

**동작 흐름**
1. Neo4j에서 node_id(키워드명)에 연결된 논문 ID 목록 조회
2. ChromaDB에서 해당 ID의 메타데이터 fetch (유사도 계산 없음)
3. Neo4j 실패(다운) 시 ChromaDB 시맨틱 검색으로 자동 fallback
4. PostgreSQL IN 쿼리 1회로 `citation_count`, `kci_registered`, `sci_indexed`, `trust_badge` 보강

**필터 파라미터**
- `year_range`: `'3y'`(2023~) / `'5y'`(2021~) / `'10y'`(2016~) / null(전체)
- `paper_type`: `'학술 저널'` / `'학위논문'` / null(전체)
- `kci`: `true`(KCI만) / `false`(비KCI만) / null(전체)
- `sci`: `true`(SCI 계열만) / null(전체) — 현재 SCI 데이터 미수집으로 true 시 결과 없을 수 있음
- `sort`: `'date'`(발행일 내림차순, 기본값) / `'citation'`(인용수 내림차순)
""",
)
async def get_node_papers(
    node_id: str,
    year_range: Optional[str] = Query(None, description="'3y'|'5y'|'10y'|null"),
    paper_type: Optional[str] = Query(None, description="'학술 저널'|'학위논문'"),
    kci: Optional[bool] = Query(None, description="KCI 등재 여부"),
    sci: Optional[bool] = Query(None, description="SCI 등재 여부"),
    sort: Literal["citation", "date"] = Query("date", description="'date'(발행일) | 'citation'(인용수)"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    service = get_chroma_search_service()
    paper_ids = await get_paper_ids_by_keyword(node_id)

    if paper_ids:
        items = await service.get_items_by_ids(paper_ids)
    else:
        items = await service.search(query=node_id, n_results=size * 2)

    items = apply_filters(items, year_range=year_range, paper_type=paper_type, kci=kci, sci=sci)
    items = apply_sort(items, sort)
    paged, total = paginate(items, page, size)

    cards = await build_paper_cards(paged, db)
    return success_response(
        data=PaperListResponse(
            keyword=node_id,
            papers=cards,
            total=total,
            page=page,
            size=size,
        ),
        message="node papers fetched",
    )


@router.post(
    "/keyword-map/generate",
    response_model=ApiResponse[KeywordMapGenerateResponse],
    summary="키워드맵 생성 및 저장",
    description="""연구 분야 텍스트로 4축 키워드 트리를 LLM으로 생성하고 저장합니다.

- `user_id` 제공 시 Redis(24시간 TTL) + PostgreSQL(`user_keyword_maps` 테이블)에 저장 (upsert)
- `user_id`가 유효한 UUID가 아니면 DB 저장 없이 Redis에만 저장
- 저장 후 `/keyword-search/map/{user_id}`로 조회 가능

**응답 `data.tree` 구조** (4축 원본)
- `root`: `{ko, en}` 최상위 연구 분야
- `axes.core_technology`: 핵심기술 노드 배열
- `axes.research_target`: 연구대상 노드 배열
- `axes.parent_domain`: 상위분야 노드 배열
- `axes.application_domain`: 응용분야 노드 배열
""",
)
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
    parent_label: str = Field(description="확장할 부모 노드 한글명", example="CNN")
    parent_label_en: str = Field("", description="부모 노드 영문명 (선택)", example="Convolutional Neural Network")
    axis: str = Field(description="부모 노드의 축. '핵심기술' | '연구대상' | '상위분야' | '응용분야'", example="핵심기술")
    research_field: str = Field(description="최상위 연구 분야 (LLM 컨텍스트용)", example="딥러닝")
    depth: int = Field(1, description="부모 노드의 현재 트리 깊이 (root=0)")


class ExpandNodeResponse(BaseModel):
    parent_label: str = Field(description="확장한 부모 노드명")
    new_children: list[dict] = Field(description="새로 생성된 하위 노드 배열 (ko, en, definition 포함)")


@router.post(
    "/keyword-map/node/{node_id}/expand",
    response_model=ApiResponse[ExpandNodeResponse],
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
