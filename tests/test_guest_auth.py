import asyncio
import time
import uuid
from datetime import datetime, timezone

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import rate_limit
from app.core.deps import get_current_member, require_demo_mode
from app.core.security import create_access_token, create_refresh_token
from app.core.settings import settings
from app.main import app
from app.models.user import User
from app.services import auth_service


def _run(coro):
    return asyncio.run(coro)


def _request(headers: dict[str, str] | None = None, client_host: str = "10.0.0.9") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (client_host, 1234)})


def _decode(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])


def _user(is_guest: bool) -> User:
    return User(
        id=uuid.uuid4(),
        email="guest_x@guest.local" if is_guest else "member@example.com",
        is_guest=is_guest,
        is_profile_set=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class _FakePipeline:
    def __init__(self, store):
        self.store, self.ops = store, []

    def set(self, key, value, ex=None, nx=False):
        self.ops.append(("set", key, value, ex, nx))

    def incr(self, key):
        self.ops.append(("incr", key))

    def ttl(self, key):
        self.ops.append(("ttl", key))

    def execute(self):
        out = []
        for op in self.ops:
            if op[0] == "set":
                _, key, value, ex, nx = op
                if nx and key in self.store:
                    out.append(None)
                else:
                    self.store[key] = [value, ex]
                    out.append(True)
            elif op[0] == "incr":
                self.store[op[1]][0] += 1
                out.append(self.store[op[1]][0])
            else:
                out.append(self.store[op[1]][1])
        return out


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def pipeline(self, transaction=True):
        return _FakePipeline(self.store)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda db: fake)
    return fake


# ── 클라이언트 IP ─────────────────────────────────────────────────────────

def test_client_ip_uses_rightmost_forwarded_value(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    req = _request({"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
    assert rate_limit.get_client_ip(req) == "203.0.113.7"


def test_client_ip_respects_proxy_hops(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    req = _request({"X-Forwarded-For": "1.1.1.1, 203.0.113.7, 10.0.0.2"})
    assert rate_limit.get_client_ip(req) == "203.0.113.7"


def test_client_ip_falls_back_to_socket_peer():
    assert rate_limit.get_client_ip(_request(client_host="192.0.2.5")) == "192.0.2.5"


# ── 토큰 ─────────────────────────────────────────────────────────────────

def test_member_tokens_have_no_guest_claim_and_default_expiry():
    uid = str(uuid.uuid4())
    access, refresh = _decode(create_access_token(uid)), _decode(create_refresh_token(uid))
    assert "guest" not in access and "guest" not in refresh
    days = (refresh["exp"] - time.time()) / 86400
    assert abs(days - settings.jwt_refresh_expire_days) < 0.01


def test_guest_refresh_token_uses_guest_expiry():
    uid = str(uuid.uuid4())
    access = _decode(create_access_token(uid, guest=True))
    refresh = _decode(create_refresh_token(uid, guest=True))
    assert access["guest"] is True and refresh["guest"] is True
    assert abs((access["exp"] - time.time()) / 60 - settings.jwt_access_expire_minutes) < 0.1
    assert abs((refresh["exp"] - time.time()) / 86400 - settings.guest_token_expire_days) < 0.01


def test_refresh_rotation_keeps_guest_claim_and_expiry(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(auth_service, "decode_token", _decode)
    monkeypatch.setattr(auth_service, "add_to_blacklist", lambda *a: None)
    uid = str(uuid.uuid4())

    result = _run(auth_service.refresh_tokens(create_refresh_token(uid, guest=True)))

    assert result["guest"] is True
    new_refresh = _decode(result["refresh_token"])
    assert new_refresh["guest"] is True
    assert abs((new_refresh["exp"] - time.time()) / 86400 - settings.guest_token_expire_days) < 0.01
    assert _decode(result["access_token"])["guest"] is True


def test_refresh_rotation_for_member_is_unchanged(monkeypatch):
    monkeypatch.setattr(auth_service, "decode_token", _decode)
    monkeypatch.setattr(auth_service, "add_to_blacklist", lambda *a: None)
    result = _run(auth_service.refresh_tokens(create_refresh_token(str(uuid.uuid4()))))
    assert result["guest"] is False
    assert "guest" not in _decode(result["refresh_token"])


def test_guest_refresh_rejected_when_demo_mode_off(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(auth_service, "decode_token", _decode)
    with pytest.raises(HTTPException) as exc:
        _run(auth_service.refresh_tokens(create_refresh_token(str(uuid.uuid4()), guest=True)))
    assert exc.value.status_code == 401


# ── 게스트 차단 / DEMO_MODE ───────────────────────────────────────────────

def test_get_current_member_blocks_guest():
    with pytest.raises(HTTPException) as exc:
        _run(get_current_member(_user(is_guest=True)))
    assert exc.value.status_code == 403


def test_get_current_member_passes_member():
    user = _user(is_guest=False)
    assert _run(get_current_member(user)) is user


def test_require_demo_mode(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    with pytest.raises(HTTPException) as exc:
        _run(require_demo_mode())
    assert exc.value.status_code == 404
    monkeypatch.setattr(settings, "demo_mode", True)
    assert _run(require_demo_mode()) is None


def test_login_rejects_guest_account(monkeypatch):
    async def fake_get_user_by_email(db, email):
        return _user(is_guest=True)

    monkeypatch.setattr(auth_service, "get_user_by_email", fake_get_user_by_email)
    with pytest.raises(HTTPException) as exc:
        _run(auth_service.login_service(None, "guest_x@guest.local", "whatever"))
    assert exc.value.status_code == 400


def test_guest_placeholder_email_is_rejected_by_login_schema():
    from pydantic import ValidationError

    from app.schemas.auth import LoginRequest

    with pytest.raises(ValidationError):
        LoginRequest(email="guest_abc@guest.local", password="x")


# ── rate limit ───────────────────────────────────────────────────────────

def test_hit_blocks_after_limit(fake_redis):
    assert rate_limit._hit("k", 2, 60) is None
    assert rate_limit._hit("k", 2, 60) is None
    assert rate_limit._hit("k", 2, 60) == 60


def test_hit_fails_open_when_redis_down(monkeypatch):
    def boom(db):
        raise ConnectionError("down")

    monkeypatch.setattr(rate_limit, "get_redis", boom)
    assert rate_limit._hit("k", 1, 60) is None


def test_llm_limit_is_noop_when_demo_mode_off(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "demo_mode", False)
    _run(rate_limit.limit_llm_calls(_request(), None))
    assert fake_redis.store == {}


def _bearer(token):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_llm_limit_counts_guest_by_user_and_skips_member(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "guest_llm_rate_limit", 1)
    monkeypatch.setattr(rate_limit, "decode_token", _decode)
    guest_id = str(uuid.uuid4())

    _run(rate_limit.limit_llm_calls(_request(), _bearer(create_access_token(guest_id, guest=True))))
    with pytest.raises(HTTPException) as exc:
        _run(rate_limit.limit_llm_calls(_request(), _bearer(create_access_token(guest_id, guest=True))))
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"]

    for _ in range(5):
        _run(rate_limit.limit_llm_calls(_request(), _bearer(create_access_token(str(uuid.uuid4())))))
    assert set(fake_redis.store) == {rate_limit.llm_guest_key(guest_id)}


def test_llm_limit_counts_anonymous_by_ip(monkeypatch, fake_redis):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "anon_llm_rate_limit_per_ip", 1)
    req = _request({"X-Forwarded-For": "203.0.113.7"})

    _run(rate_limit.limit_llm_calls(req, None))
    with pytest.raises(HTTPException):
        _run(rate_limit.limit_llm_calls(req, None))
    _run(rate_limit.limit_llm_calls(_request({"X-Forwarded-For": "203.0.113.8"}), None))


# ── Swagger ──────────────────────────────────────────────────────────────

def test_guest_route_in_openapi():
    paths = app.openapi()["paths"]
    op = paths["/api/v1/auth/guest"]["post"]
    assert {"200", "404", "429"} <= set(op["responses"])
    assert op["responses"]["200"]["content"]["application/json"]["schema"]


def test_llm_routes_have_rate_limit_dependency():
    llm_paths = {
        "/api/v1/search/papers",
        "/api/v1/search/selection-reasons",
        "/api/v1/search/chat",
        "/api/v1/search/chat/stream",
        "/api/v1/keyword-map/node/{node_key:path}/detail",
        "/api/v1/researchers/{researcher_id}/research-flow",
    }
    found = set()
    for route in app.routes:
        if getattr(route, "path", None) in llm_paths:
            if any(d.call is rate_limit.limit_llm_calls for d in route.dependant.dependencies):
                found.add(route.path)
    assert found == llm_paths
