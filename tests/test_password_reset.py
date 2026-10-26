import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.security import verify_password
from app.api.v1.auth import router as auth_router
from app.schemas.auth import PasswordResetConfirmRequest, RegisterRequest
from app.services import auth_service


def _run(coro):
    return asyncio.run(coro)


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key: str):
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self.store[key] = str(value)
        self.ttls[key] = ttl

    def delete(self, *keys: str):
        for key in keys:
            self.store.pop(key, None)
            self.ttls.pop(key, None)


def _email_user(password_hash: str = "hashed"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="member@example.com",
        hashed_password=password_hash,
        is_guest=False,
    )


def test_password_reset_schema_uses_registration_password_policy():
    valid = "ValidPass1!"
    assert RegisterRequest(
        email="member@example.com", password=valid, confirm_password=valid
    )
    assert PasswordResetConfirmRequest(
        email="member@example.com",
        new_password=valid,
        confirm_password=valid,
    )

    with pytest.raises(ValidationError):
        PasswordResetConfirmRequest(
            email="member@example.com",
            new_password="weak",
            confirm_password="weak",
        )
    with pytest.raises(ValidationError):
        PasswordResetConfirmRequest(
            email="member@example.com",
            new_password=valid,
            confirm_password="Different1!",
        )


def test_signup_and_password_reset_codes_use_separate_keys(monkeypatch):
    redis = FakeRedis()
    sent_codes: list[tuple[str, str]] = []

    monkeypatch.setattr(auth_service, "generate_code", lambda: "123456")

    async def fake_send(email: str, code: str):
        sent_codes.append((email, code))

    monkeypatch.setattr(auth_service, "send_verification_email", fake_send)

    _run(auth_service.send_code_service(redis, "member@example.com"))
    _run(
        auth_service.send_code_service(
            redis, "member@example.com", purpose="password_reset"
        )
    )

    assert redis.store["code:member@example.com"] == "123456"
    assert redis.store["code:password_reset:member@example.com"] == "123456"
    assert redis.ttls["code:password_reset:member@example.com"] == 900
    assert len(sent_codes) == 2


def test_password_reset_verification_is_single_purpose():
    redis = FakeRedis()
    redis.setex("code:password_reset:member@example.com", 900, "123456")

    _run(
        auth_service.verify_password_reset_code_service(
            redis, "member@example.com", "123456"
        )
    )

    assert "code:password_reset:member@example.com" not in redis.store
    assert redis.store["verify:password_reset:member@example.com"] == "1"
    assert "verify:member@example.com" not in redis.store


def test_signup_verification_keeps_legacy_keys():
    redis = FakeRedis()
    email = "member@example.com"
    redis.setex(f"code:{email}", 900, "123456")

    _run(auth_service.verify_code_service(redis, email, "123456"))

    assert redis.store[f"verify:{email}"] == "1"
    assert f"verify:password_reset:{email}" not in redis.store


def test_fifth_failed_attempt_invalidates_password_reset_code():
    redis = FakeRedis()
    code_key = "code:password_reset:member@example.com"
    redis.setex(code_key, 900, "123456")

    for _ in range(4):
        with pytest.raises(HTTPException) as exc:
            _run(
                auth_service.verify_password_reset_code_service(
                    redis, "member@example.com", "000000"
                )
            )
        assert exc.value.detail == "인증번호가 일치하지 않습니다"

    with pytest.raises(HTTPException) as exc:
        _run(
            auth_service.verify_password_reset_code_service(
                redis, "member@example.com", "000000"
            )
        )
    assert exc.value.detail == "인증 시도 횟수를 초과했습니다. 인증번호를 재발송해주세요"
    assert code_key not in redis.store


def test_send_password_reset_code_hides_non_password_account(monkeypatch):
    async def fake_get_user(db, email):
        return SimpleNamespace(hashed_password=None, is_guest=False)

    monkeypatch.setattr(auth_service, "get_user_by_email", fake_get_user)

    redis = FakeRedis()
    result = _run(
        auth_service.send_password_reset_code_service(
            None, redis, "social@example.com"
        )
    )
    assert result["message"] == "인증번호가 발송되었습니다"
    assert redis.store == {}


def test_reset_password_requires_verification(monkeypatch):
    redis = FakeRedis()

    with pytest.raises(HTTPException) as exc:
        _run(
            auth_service.reset_password_service(
                None, redis, "member@example.com", "NewPassword1!"
            )
        )
    assert exc.value.detail == "비밀번호 재설정을 위한 이메일 인증이 필요합니다"


def test_reset_password_updates_hash_and_consumes_verification(monkeypatch):
    redis = FakeRedis()
    email = "member@example.com"
    verify_key = f"verify:password_reset:{email}"
    redis.setex(verify_key, 900, "1")
    user = _email_user()

    async def fake_get_user(db, requested_email):
        assert requested_email == email
        return user

    async def fake_update_user(db, target, **kwargs):
        target.hashed_password = kwargs["hashed_password"]
        return target

    monkeypatch.setattr(auth_service, "get_user_by_email", fake_get_user)
    monkeypatch.setattr(auth_service, "update_user", fake_update_user)

    result = _run(
        auth_service.reset_password_service(
            None, redis, email, "NewPassword1!"
        )
    )

    assert result["message"] == "비밀번호가 재설정되었습니다"
    assert verify_password("NewPassword1!", user.hashed_password)
    assert verify_key not in redis.store

    assert _run(auth_service.login_service(None, email, "NewPassword1!"))[
        "token_type"
    ] == "bearer"
    with pytest.raises(HTTPException):
        _run(auth_service.login_service(None, email, "OldPassword1!"))


def test_password_reset_routes_are_documented():
    expected = {
        "/auth/password-reset/send-code",
        "/auth/password-reset/verify-code",
        "/auth/password-reset/confirm",
    }
    paths = {route.path for route in auth_router.routes}
    assert expected <= paths
