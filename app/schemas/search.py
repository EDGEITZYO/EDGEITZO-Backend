from typing import Literal, Optional

from pydantic import BaseModel, Field


class SearchPapersRequest(BaseModel):
    query: str = Field(..., min_length=1, description="사용자 검색어")
    paper_scope: Literal["kci", "international", "both", "any"] = Field(
        default="any",
        description="논문 범위",
    )
    time_range: Literal["3y", "5y", "10y", "all", "skip"] = Field(
        default="skip",
        description="발행 시기 범위",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="추출/선택된 키워드 목록",
    )
    page: int = Field(default=1, ge=1, description="페이지 번호")
    size: int = Field(default=10, ge=1, le=30, description="페이지 크기")


class PaperAuthor(BaseModel):
    name: str
    affiliation: Optional[str] = None


class CredibilityInfo(BaseModel):
    badge: Literal["high", "medium", "low", "unknown"] = "unknown"
    citation_count: Optional[int] = None
    citation_badge: Optional[str] = None
    impact_factor: Optional[float] = None
    impact_factor_badge: Optional[str] = None
    kci_registered: Optional[bool] = None
    kci_badge: Optional[str] = None
    sci_indexed: Optional[bool] = None
    sci_badge: Optional[str] = None
    sjr_quartile: Optional[str] = None
    sjr_score: Optional[float] = None
    h_index: Optional[int] = None
    summary: Optional[str] = None


class PaperSearchItem(BaseModel):
    paper_id: str
    title: str
    authors: list[PaperAuthor] = Field(default_factory=list)
    year: Optional[int] = None
    abstract: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    journal_name: Optional[str] = None
    issn: Optional[str] = None
    doi: Optional[str] = None
    db_code: Optional[str] = None
    source: str
    credibility: CredibilityInfo
    score: float


class SearchPapersResponse(BaseModel):
    search_id: str
    items: list[PaperSearchItem]
