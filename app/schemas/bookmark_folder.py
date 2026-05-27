from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BookmarkFolderCreate(BaseModel):
    name: str = Field(..., max_length=100)


class BookmarkFolderUpdate(BaseModel):
    name: str = Field(..., max_length=100)


class BookmarkFolderResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    paper_count: int = 0
    representative_keywords: list[str] = []
    updated_at: Optional[datetime] = None   # 폴더 내 마지막 북마크 시각

    model_config = {"from_attributes": True}
