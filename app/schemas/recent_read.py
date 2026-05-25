from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RecentReadItem(BaseModel):
    paper_id: str
    title: str
    title_en: Optional[str] = None
    abstract: Optional[str] = None
    abstract_en: Optional[str] = None
    doi: Optional[str] = None
    issn: Optional[str] = None
    pubyear: Optional[int] = None
    source_type: Optional[str] = None
    db_code: Optional[str] = None
    journal_name: Optional[str] = None
    journal_issn: list[str] = Field(default_factory=list)
    read_at: datetime


class RecentReadsResponse(BaseModel):
    total_count: int = Field(default=0, ge=0)
    items: list[RecentReadItem] = Field(default_factory=list)
