from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class BookmarkCreate(BaseModel):
    paper_id: str
    folder_id: Optional[UUID] = None


class BookmarkResponse(BaseModel):
    id: UUID
    paper_id: str
    folder_id: Optional[UUID]
    created_at: datetime

    model_config = {"from_attributes": True}


class BookmarkCheckResponse(BaseModel):
    paper_id: str
    bookmarked: bool
