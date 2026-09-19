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
    get_budget_status,
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

    차단 기준은 `budget_mode`에 따라 다르다.
    - `prepaid`(LLM_BUDGET_PREPAID_USD 설정 시): 충전 예산을 켠 뒤로 쓴 금액. **리셋되지 않는다**
      — 다시 충전하면 설정값을 올려야 풀린다. `next_reset_date`는 null.
    - `monthly`: 이번 달 사용액. 다음 달 1일(`next_reset_date`)에 저절로 회복된다.
    한도에 닿으면 모든 LLM 기능이(선정 사유·AI 요약·키워드 추출) 조용히 멈추므로, 사유가 전부
    null로 내려온다면 여기부터 확인할 것.

    `monthly_cost_usd`(이번 달)와 `total_cost_usd`(평생)는 방식과 무관하게 관측용으로 항상 준다.
    """
    status_ = get_budget_status()
    limit = status_["limit_usd"]
    return success_response(
        data={
            "budget_mode": status_["mode"],
            "used_usd": status_["used_usd"],
            "remaining_budget_usd": status_["remaining_usd"],
            "budget_limit_usd": limit,
            "usage_ratio": round(status_["used_usd"] / limit, 4) if limit else None,
            "exhausted": status_["exhausted"],
            "next_reset_date": None if status_["mode"] == "prepaid" else next_reset_date().isoformat(),
            "monthly_cost_usd": await get_monthly_cost(),
            "total_cost_usd": await get_total_cost(),  # 평생 누적 (관측용)
        },
        message="LLM 비용 조회 완료",
    )


@router.post("/health/llm-cost/reset")
async def reset_llm_cost():
    """이번 달 LLM 비용 카운터 초기화 — 한도에 걸린 걸 즉시 풀어야 할 때.

    다음 달 1일이면 저절로 풀리므로 평소엔 부를 일이 없다. 평생 누적(total_cost_usd)은
    별개의 기록이라 지우지 않는다. 충전 예산 카운터도 지우지 않는다 — 충전한 만큼만 쓰는 게
    그 방식의 목적이라, 리셋으로 풀 수 있으면 결제 잔액을 넘겨 쓸 수 있게 된다.
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
