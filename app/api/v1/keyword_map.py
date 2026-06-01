from __future__ import annotations

import json
import uuid as _uuid

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.response import success_response
from app.models.user_keyword_map import UserKeywordMap
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
