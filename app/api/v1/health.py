import httpx
from fastapi import APIRouter
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.neo4j_client import get_neo4j_driver
from app.core.redis import get_redis
from app.core.redis_client import get_redis_client
from app.core.response import success_response
from app.core.settings import settings
from app.services.llm.client import (
    get_monthly_cost,
    get_remaining_budget,
    get_total_cost,
    next_reset_date,
    reset_cost,
)

router = APIRouter()


@router.get("/health")
async def health_check():
    redis_status = "down"
    neo4j_status = "down"
    postgres_status = "down"
    chromadb_status = "down"

    try:
        redis_client = get_redis_client()
        redis_client.ping()
        redis_status = "up"
    except Exception:
        pass

    try:
        driver = get_neo4j_driver()
        with driver.session() as session:
            session.run("RETURN 1")
        neo4j_status = "up"
        driver.close()
    except Exception:
        pass

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        postgres_status = "up"
    except Exception:
        pass

    try:
        chroma_base_url = f"http://{settings.chroma_host}:{settings.chroma_port}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            for path in ("/api/v2/heartbeat", "/api/v1/heartbeat"):
                response = await client.get(f"{chroma_base_url}{path}")
                if response.status_code == 200:
                    chromadb_status = "up"
                    break
    except Exception:
        pass

    all_up = all(
        s == "up" for s in [redis_status, neo4j_status, postgres_status, chromadb_status]
    )
    overall_status = "ok" if all_up else "partial"

    return success_response(
        data={
            "status": overall_status,
            "service": "PaperGraph API",
            "redis": redis_status,
            "neo4j": neo4j_status,
            "postgres": postgres_status,
            "chromadb": chromadb_status,
        },
        message="health check completed",
    )


@router.get("/health/llm-cost")
async def llm_cost():
    """LLM 비용·잔여 예산 조회.

    차단 기준은 `monthly_cost_usd`다. 이게 `budget_limit_usd`에 닿으면 모든 LLM 기능이
    (선정 사유·AI 요약·키워드 추출) 조용히 멈추므로, 사유가 전부 null로 내려온다면 여기부터
    확인할 것. `next_reset_date`에 저절로 회복된다.

    `total_cost_usd`는 평생 누적으로 관측용이며 차단과 무관하다.
    """
    monthly = await get_monthly_cost()
    limit = settings.llm_budget_monthly_usd
    return success_response(
        data={
            "monthly_cost_usd": monthly,
            "remaining_budget_usd": await get_remaining_budget(),
            "budget_limit_usd": limit,
            "usage_ratio": round(monthly / limit, 4) if limit else None,
            "exhausted": monthly >= limit,
            "next_reset_date": next_reset_date().isoformat(),
            "total_cost_usd": await get_total_cost(),  # 평생 누적 (관측용)
        },
        message="LLM 비용 조회 완료",
    )


@router.post("/health/llm-cost/reset")
async def reset_llm_cost():
    """이번 달 LLM 비용 카운터 초기화 — 한도에 걸린 걸 즉시 풀어야 할 때.

    다음 달 1일이면 저절로 풀리므로 평소엔 부를 일이 없다. 평생 누적(total_cost_usd)은
    별개의 기록이라 지우지 않는다.
    """
    await reset_cost()
    return success_response(
        data={"reset": True, "monthly_cost_usd": await get_monthly_cost()},
        message="이번 달 LLM 비용 카운터 초기화 완료",
    )


@router.post("/health/recent-searches/reset")
async def reset_recent_searches():
    """전체 유저 최근 탐색 이력 초기화 — 탐색 이력 구조 변경 배포 시 호출"""
    r = get_redis(7)
    keys = r.keys("recent_searches:*")
    if keys:
        r.delete(*keys)
    return success_response(data={"deleted_keys": len(keys)}, message=f"최근 탐색 이력 {len(keys)}건 초기화 완료")
