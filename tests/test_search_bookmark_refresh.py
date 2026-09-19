"""채팅 검색 응답의 북마크 여부를 응답마다 다시 채우는지 (세션에 저장된 옛 값을 쓰지 않는지)."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import search as search_api


class _Session:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _item(pid, flag):
    return SimpleNamespace(paper_id=pid, is_bookmarked=flag)


@pytest.mark.asyncio
async def test_refresh_overrides_stale_flags_including_history(monkeypatch):
    monkeypatch.setattr(search_api, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(search_api, "get_bookmarked_paper_ids", AsyncMock(return_value={"B"}))
    response = SimpleNamespace(
        result_items=[_item("A", True), _item("B", False)],  # 세션에 저장될 때의 옛 값
        history=[SimpleNamespace(result_items=[_item("A", True)])],
    )
    await search_api._refresh_bookmarks(response, SimpleNamespace(id=uuid.uuid4()))
    assert [i.is_bookmarked for i in response.result_items] == [False, True]
    assert response.history[0].result_items[0].is_bookmarked is False


@pytest.mark.asyncio
async def test_refresh_clears_flags_for_anonymous(monkeypatch):
    lookup = AsyncMock()
    monkeypatch.setattr(search_api, "get_bookmarked_paper_ids", lookup)
    response = SimpleNamespace(result_items=[_item("A", True)], history=[])
    await search_api._refresh_bookmarks(response, None)
    assert response.result_items[0].is_bookmarked is False
    lookup.assert_not_called()
