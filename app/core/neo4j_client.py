import atexit
import threading

from neo4j import GraphDatabase

from app.core.settings import settings

# 드라이버는 프로세스당 하나만 두고 재사용한다. 호출할 때마다 새로 만들면 매 요청에
# TCP 연결 + 인증 핸드셰이크가 발생하고 내부 커넥션 풀도 매번 버려진다
# (검색 한 턴에서 _build_expand_chips가 이 비용을 그대로 물고 있었음).
_driver = None
_lock = threading.Lock()


class _SharedDriver:
    """close()를 무시하는 드라이버 프록시.

    기존 호출부 12곳이 `try/finally: driver.close()` 형태로 드라이버를 닫고 있다.
    공용 드라이버를 그대로 넘기면 먼저 끝난 호출자가 남의 커넥션 풀까지 닫아버리므로,
    close()만 무시하고 나머지 속성은 그대로 위임한다.
    실제 종료는 close_neo4j_driver() 또는 atexit이 담당한다.
    """

    __slots__ = ("_driver",)

    def __init__(self, driver):
        object.__setattr__(self, "_driver", driver)

    def close(self) -> None:  # noqa: D102 - 의도적 no-op
        return None

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_driver"), name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def get_neo4j_driver():
    """공용 Neo4j 드라이버. close()를 불러도 실제로 닫히지 않는다(위 프록시 참고)."""
    global _driver
    if _driver is None:
        with _lock:
            if _driver is None:
                _driver = _SharedDriver(
                    GraphDatabase.driver(
                        settings.neo4j_uri,
                        auth=(settings.neo4j_user, settings.neo4j_password),
                    )
                )
    return _driver


def close_neo4j_driver() -> None:
    """프로세스 종료 시 실제로 드라이버를 닫는다."""
    global _driver
    if _driver is not None:
        inner = object.__getattribute__(_driver, "_driver")
        _driver = None
        try:
            inner.close()
        except Exception:
            pass


atexit.register(close_neo4j_driver)
