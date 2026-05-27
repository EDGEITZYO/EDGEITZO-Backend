"""북마크 서비스 단위 테스트 (AsyncMock)."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.bookmark import Bookmark
from app.services.bookmark_service import add_bookmark, check_bookmark, remove_bookmark

_USER_ID = uuid.uuid4()
_PAPER_ID = "TEST_PAPER_001"


def _make_db(existing: Bookmark | None = None) -> AsyncMock:
    """scalar_one_or_none가 existing을 반환하는 mock session."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    result_mock.scalar_one.return_value = existing

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_add_bookmark_new():
    db = _make_db(existing=None)
    bm = await add_bookmark(db, _USER_ID, _PAPER_ID)
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    assert bm.paper_id == _PAPER_ID
    assert bm.user_id == _USER_ID


@pytest.mark.asyncio
async def test_add_bookmark_idempotent():
    existing = Bookmark(id=uuid.uuid4(), user_id=_USER_ID, paper_id=_PAPER_ID)
    db = _make_db(existing=existing)
    bm = await add_bookmark(db, _USER_ID, _PAPER_ID)
    db.add.assert_not_called()  # 이미 있으면 insert 안 함
    assert bm.id == existing.id


@pytest.mark.asyncio
async def test_remove_bookmark_exists():
    existing = Bookmark(id=uuid.uuid4(), user_id=_USER_ID, paper_id=_PAPER_ID)
    db = _make_db(existing=existing)
    result = await remove_bookmark(db, _USER_ID, _PAPER_ID)
    assert result is True
    db.delete.assert_awaited_once_with(existing)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_remove_bookmark_not_found():
    db = _make_db(existing=None)
    result = await remove_bookmark(db, _USER_ID, _PAPER_ID)
    assert result is False
    db.delete.assert_not_called()


@pytest.mark.asyncio
async def test_check_bookmark_true():
    existing = Bookmark(id=uuid.uuid4(), user_id=_USER_ID, paper_id=_PAPER_ID)
    db = _make_db(existing=existing)
    assert await check_bookmark(db, _USER_ID, _PAPER_ID) is True


@pytest.mark.asyncio
async def test_check_bookmark_false():
    db = _make_db(existing=None)
    assert await check_bookmark(db, _USER_ID, _PAPER_ID) is False
