"""
Celery 인스턴스.

브로커/백엔드 모두 Redis 사용 (DB 1/2번).
태스크는 app/tasks/ 하위 모듈에서 자동 발견.
"""
from celery import Celery

from app.core.settings import settings

celery_app = Celery(
    "edgeitzo",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.health",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Seoul",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=240,
    worker_max_tasks_per_child=100,
    result_expires=3600,
)

if __name__ == "__main__":
    celery_app.start()
