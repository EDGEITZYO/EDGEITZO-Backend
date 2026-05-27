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


class BookmarkedPaper(BaseModel):
    id: str
    title: str
    authors: Optional[list[str]] = None
    pubdate: Optional[str] = None       # pubyear → "YYYY" 문자열 (월/일 데이터 없음)
    paper_type: Optional[str] = None
    doi: Optional[str] = None
    citation_count: int = 0
    abstract: Optional[str] = None
    keywords: Optional[list[str]] = None
    journal_name: Optional[str] = None
    kci_status: Optional[str] = None
    sjr_quartile: Optional[str] = None

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
