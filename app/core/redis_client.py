from app.core.redis import get_redis


def get_redis_client():
    return get_redis(0)
