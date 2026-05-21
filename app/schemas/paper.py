from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.search import CredibilityInfo


class PaperDetailJournal(BaseModel):
    name: Optional[str] = Field(default=None, description="Journal name")
    issn: list[str] = Field(default_factory=list, description="Journal ISSN values")


class PaperDetailKeyword(BaseModel):
    key: Optional[str] = Field(default=None, description="Keyword node key")
    name: str = Field(..., description="Keyword display name")
    normalized_name: Optional[str] = Field(default=None, description="Normalized keyword name")
    lang: Optional[str] = Field(default=None, description="Keyword language, ko or en")
    source_field: Optional[str] = Field(default=None, description="Original keyword source field")


class PaperDetailResponse(BaseModel):
    paper_cn: str = Field(..., description="ScienceON paper CN")
    db_code: Optional[str] = Field(default=None, description="ScienceON DB code")
    title: Optional[str] = Field(default=None, description="Korean paper title")
    title_en: Optional[str] = Field(default=None, description="English paper title")
    display_title: str = Field(..., description="Frontend display title")
    abstract: Optional[str] = Field(default=None, description="Korean abstract")
    abstract_en: Optional[str] = Field(default=None, description="English abstract")
    display_abstract: Optional[str] = Field(default=None, description="Frontend display abstract")
    doi: Optional[str] = Field(default=None, description="DOI identifier")
    original_url: Optional[str] = Field(default=None, description="Original paper URL")
    pubyear: Optional[int] = Field(default=None, description="Publication year")
    journal_name: Optional[str] = Field(default=None, description="Journal name")
    journal: Optional[PaperDetailJournal] = None
    issn: list[str] = Field(default_factory=list, description="Paper ISSN values")
    authors: list[str] = Field(default_factory=list, description="Author names")
    author_display: Optional[str] = Field(default=None, description="Collapsed author display")
    author_count: int = Field(default=0, ge=0, description="Author count")
    keywords: list[PaperDetailKeyword] = Field(default_factory=list)
    core_keywords: list[str] = Field(default_factory=list, description="Keyword display names")
    credibility: CredibilityInfo
