import redis
from app.core.settings import settings


def get_redis_client():
    client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True
    )
    return client