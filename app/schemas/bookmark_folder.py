from datetime import datetime
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

    model_config = {"from_attributes": True}
