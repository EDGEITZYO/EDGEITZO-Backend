from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.schemas.trust_badge import TrustBadge


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


class BookmarkedPaper(BaseModel):
    id: str
    paper_type: Optional[str] = None       # journal | conference | thesis_phd | thesis_master
    title: str
    authors: Optional[list[str]] = None
    published_at: Optional[str] = None     # ISO8601
    doi: Optional[str] = None
    citation_count: int = 0
    abstract: Optional[str] = None
    keywords: Optional[list[str]] = None
    journal_name: Optional[str] = None
    trust_badge: Optional[TrustBadge] = None
    related_papers: list = []

    model_config = {"from_attributes": True}


class BookmarkListItem(BaseModel):
    bookmark_id: UUID
    folder_id: Optional[UUID]
    bookmarked_at: datetime
    paper: BookmarkedPaper

    model_config = {"from_attributes": True}


class BookmarkListResponse(BaseModel):
    total: int
    page: int
    size: int
    items: list[BookmarkListItem]
