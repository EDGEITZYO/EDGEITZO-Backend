import redis

from app.core.settings import settings

# DB별 connection pool (DB 번호 → pool 인스턴스)
_pools: dict[int, redis.ConnectionPool] = {}


def get_redis(db: int) -> redis.Redis:
    """DB 번호로 Redis 클라이언트 반환 (pool 재사용)"""
    if db not in _pools:
        _pools[db] = redis.ConnectionPool(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password or None,
            db=db,
            decode_responses=True,
        )
    return redis.Redis(connection_pool=_pools[db])
