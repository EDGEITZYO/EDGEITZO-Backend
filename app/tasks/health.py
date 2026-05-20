"""헬스체크용 태스크. Celery 동작 검증 전용."""
from app.celery_app import celery_app


@celery_app.task(name="health.ping")
def ping() -> dict:
    """가장 단순한 태스크. 호출되면 pong 반환."""
    return {"status": "ok", "message": "pong"}


@celery_app.task(name="health.echo")
def echo(message: str) -> dict:
    """입력을 그대로 반환. 인자 전달 동작 검증용."""
    return {"status": "ok", "echo": message}


@celery_app.task(name="health.add", bind=True)
def add(self, x: int, y: int) -> dict:
    """간단한 계산 태스크. bind=True로 self(task instance) 접근 가능."""
    return {
        "status": "ok",
        "task_id": self.request.id,
        "result": x + y,
    }
