from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.redis import get_redis
from app.core.response import success_response
from app.services.keyword_map_service import generate_keyword_map

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


@router.post("/keyword-map/generate")
async def generate_map(request: KeywordMapRequest):
    """연구분야 텍스트 → 4축 키워드 트리 JSON 생성"""
    tree = await generate_keyword_map(request.research_field)

    # 사용자 ID가 있으면 Redis 캐시 저장 (keyword_search 조회용)
    if request.user_id:
        r = get_redis(_REDIS_DB)
        r.set(
            f"keyword_map:{request.user_id}",
            json.dumps({"research_field": request.research_field, "tree": tree}, ensure_ascii=False),
            ex=_MAP_TTL,
        )

    return success_response(
        data=KeywordMapGenerateResponse(
            research_field=request.research_field,
            tree=tree,
        ),
        message="keyword map generated",
    )
