"""북마크 서비스 단위 테스트 (AsyncMock)."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.bookmark import Bookmark
from app.services import bookmark_service as svc
from app.services.bookmark_service import BookmarkTargetNotFound, add_bookmark, check_bookmark, remove_bookmark

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


def _sql(call) -> str:
    return str(call.args[0].compile(compile_kwargs={"literal_binds": False}))


@pytest.fixture
def paper_exists(monkeypatch):
    """논문 존재 확인은 별도 테스트에서 검증하고, 여기선 항상 있는 것으로 둔다."""
    monkeypatch.setattr(svc, "resolve_bookmark_paper_id", AsyncMock(side_effect=lambda db, pid: pid))


@pytest.mark.asyncio
async def test_add_bookmark_new_without_folder(paper_exists):
    """폴더 없이 추가: INSERT ... ON CONFLICT DO NOTHING 후 조회. 이미 있으면 기존 폴더를 건드리지 않는다."""
    created = Bookmark(id=uuid.uuid4(), user_id=_USER_ID, paper_id=_PAPER_ID)
    db = _make_db(existing=created)

    bm = await add_bookmark(db, _USER_ID, _PAPER_ID)

    assert db.execute.await_count == 2  # insert, 그다음 select
    assert "DO NOTHING" in _sql(db.execute.await_args_list[0])
    db.commit.assert_awaited_once()
    db.add.assert_not_called()
    assert bm.paper_id == _PAPER_ID


@pytest.mark.asyncio
async def test_add_bookmark_with_folder_moves_existing(paper_exists):
    """이미 북마크된 논문을 다른 폴더로 다시 저장하면 폴더를 옮긴다(예전엔 200인데 안 바뀜)."""
    folder_id = uuid.uuid4()
    existing = Bookmark(id=uuid.uuid4(), user_id=_USER_ID, paper_id=_PAPER_ID, folder_id=folder_id)
    db = _make_db(existing=existing)
    db.execute.return_value.scalar.return_value = folder_id  # 본인 폴더 확인 통과

    await add_bookmark(db, _USER_ID, _PAPER_ID, folder_id)

    assert db.execute.await_count == 3  # 폴더 소유 확인, upsert, select
    assert "DO UPDATE SET folder_id" in _sql(db.execute.await_args_list[1])


@pytest.mark.asyncio
async def test_add_bookmark_rejects_missing_or_foreign_folder(paper_exists):
    """없는 폴더·남의 폴더는 저장하지 않는다(예전엔 없는 폴더 500, 남의 폴더는 저장됨)."""
    db = _make_db(existing=None)
    db.execute.return_value.scalar.return_value = None
    with pytest.raises(BookmarkTargetNotFound):
        await add_bookmark(db, _USER_ID, _PAPER_ID, uuid.uuid4())
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_add_bookmark_rejects_missing_paper(monkeypatch):
    monkeypatch.setattr(svc, "resolve_bookmark_paper_id", AsyncMock(return_value=None))
    db = _make_db(existing=None)
    with pytest.raises(BookmarkTargetNotFound):
        await add_bookmark(db, _USER_ID, "JAKO000000000000000")
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_loads_unloaded_domestic_paper(monkeypatch):
    """서비스 DB에 없는 국내 논문(ART…, 인용관계 그래프 카드)은 KCI에서 적재한 뒤 북마크한다."""
    materialize = AsyncMock(return_value="ART001714580")
    monkeypatch.setattr(svc, "materialize_domestic_paper", materialize)
    db = _make_db(existing=None)
    db.execute.return_value.scalar.return_value = None  # papers에 없음

    assert await svc.resolve_bookmark_paper_id(db, "ART001714580") == "ART001714580"
    materialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_does_not_fetch_unknown_non_kci_id(monkeypatch):
    materialize = AsyncMock()
    monkeypatch.setattr(svc, "materialize_domestic_paper", materialize)
    db = _make_db(existing=None)
    db.execute.return_value.scalar.return_value = None

    assert await svc.resolve_bookmark_paper_id(db, "JAKO000000000000000") is None
    materialize.assert_not_called()


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
