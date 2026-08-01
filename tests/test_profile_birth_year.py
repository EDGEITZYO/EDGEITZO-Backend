import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.user import User
from app.schemas.auth import ProfileCreateRequest
from app.schemas.mypage import MypageProfileUpdate
from app.services.mypage_service import _profile_from_user


def test_profile_create_request_uses_birth_year():
    request = ProfileCreateRequest(name="tester", birth_year=2001)

    data = request.model_dump(exclude_none=True)

    assert data["birth_year"] == 2001
    assert "age" not in data


def test_mypage_profile_response_includes_birth_year():
    user = User(
        id=uuid.uuid4(),
        email="tester@example.com",
        provider="email",
        name="tester",
        birth_year=2001,
        is_profile_set=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    profile = _profile_from_user(user)
    data = profile.model_dump()

    assert data["birth_year"] == 2001
    assert "age" not in data


def test_profile_update_rejects_future_birth_year():
    with pytest.raises(ValidationError):
        MypageProfileUpdate(birth_year=datetime.now().year + 1)
