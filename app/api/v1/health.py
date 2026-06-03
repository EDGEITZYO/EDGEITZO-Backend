import httpx
from fastapi import APIRouter
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.neo4j_client import get_neo4j_driver
from app.core.redis_client import get_redis_client
from app.core.response import success_response
from app.core.settings import settings

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
