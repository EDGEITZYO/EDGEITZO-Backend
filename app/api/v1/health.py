from fastapi import APIRouter

from app.core.neo4j_client import get_neo4j_driver
from app.core.redis_client import get_redis_client
from app.core.response import success_response

router = APIRouter()


@router.get("/health")
def health_check():
    redis_status = "down"
    neo4j_status = "down"

    try:
        redis_client = get_redis_client()
        redis_client.ping()
        redis_status = "up"
    except Exception:
        redis_status = "down"

    try:
        driver = get_neo4j_driver()
        with driver.session() as session:
            session.run("RETURN 1")
        neo4j_status = "up"
        driver.close()
    except Exception:
        neo4j_status = "down"

    overall_status = "ok" if redis_status == "up" and neo4j_status == "up" else "partial"

    return success_response(
        data={
            "status": overall_status,
            "service": "PaperGraph API",
            "redis": redis_status,
            "neo4j": neo4j_status,
        },
        message="health check completed",
    )