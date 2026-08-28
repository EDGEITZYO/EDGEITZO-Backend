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
    """신규 추가는 INSERT ... ON CONFLICT DO NOTHING 1회 + 조회 1회로 처리된다.

    ORM의 db.add()를 쓰던 구현이 upsert로 바뀌었으므로(b87a935), 여기서 검증할 것은
    'add가 호출됐는가'가 아니라 'execute가 insert→select 두 번 돌고 커밋됐는가'다.
    """
    created = Bookmark(id=uuid.uuid4(), user_id=_USER_ID, paper_id=_PAPER_ID)
    db = _make_db(existing=created)

    bm = await add_bookmark(db, _USER_ID, _PAPER_ID)

    assert db.execute.await_count == 2  # insert, 그다음 select
    db.commit.assert_awaited_once()
    db.add.assert_not_called()  # upsert 경로라 ORM add를 쓰지 않는다
    assert bm.paper_id == _PAPER_ID
    assert bm.user_id == _USER_ID


@pytest.mark.asyncio
async def test_add_bookmark_idempotent():
    """이미 있으면 ON CONFLICT DO NOTHING이 삽입을 건너뛰고 기존 행을 그대로 돌려준다."""
    existing = Bookmark(id=uuid.uuid4(), user_id=_USER_ID, paper_id=_PAPER_ID)
    db = _make_db(existing=existing)
    bm = await add_bookmark(db, _USER_ID, _PAPER_ID)
    db.add.assert_not_called()
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
