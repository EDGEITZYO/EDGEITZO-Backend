from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class GraphKeywordNode(BaseModel):
    key: str = Field(..., description="Keyword node unique key, e.g. ko:생명공학")
    name: str = Field(..., description="Keyword display name")
    normalized_name: Optional[str] = Field(default=None, description="Normalized keyword name")
    lang: Optional[str] = Field(default=None, description="Keyword language, ko or en")
    source_field: Optional[str] = Field(default=None, description="Original field name")
    paper_count: int = Field(default=0, ge=0, description="Number of connected papers")
    is_center: bool = Field(default=False, description="Whether this is the requested keyword")


class GraphKeywordEdge(BaseModel):
    source: str = Field(..., description="Source keyword key")
    target: str = Field(..., description="Target keyword key")
    paper_count: int = Field(default=0, ge=0, description="Co-occurrence paper count")
    lang_pair: Optional[str] = Field(default=None, description="Language pair of connected keywords")


class KeywordGraphResponse(BaseModel):
    center: GraphKeywordNode
    nodes: list[GraphKeywordNode] = Field(default_factory=list)
    edges: list[GraphKeywordEdge] = Field(default_factory=list)


class GraphPaperItem(BaseModel):
    cn: str = Field(..., description="Paper unique CN")
    db_code: Optional[str] = Field(default=None, description="ScienceON DB code")
    title: Optional[str] = Field(default=None, description="Korean paper title")
    title_en: Optional[str] = Field(default=None, description="English paper title")
    abstract: Optional[str] = Field(default=None, description="Korean abstract")
    abstract_en: Optional[str] = Field(default=None, description="English abstract")
    doi: Optional[str] = Field(default=None, description="DOI URL or identifier")
    pubyear: Optional[int] = Field(default=None, description="Publication year")
    journal_name: Optional[str] = Field(default=None, description="Journal name")
    authors: list[str] = Field(default_factory=list, description="Author names")
    keywords: list[str] = Field(default_factory=list, description="Keyword display names")
    keyword_keys: list[str] = Field(default_factory=list, description="Keyword node keys")


class KeywordPapersResponse(BaseModel):
    keyword: GraphKeywordNode
    total_count: int = Field(default=0, ge=0, description="Total papers connected to keyword")
    items: list[GraphPaperItem] = Field(default_factory=list)


class PaperGraphNode(BaseModel):
    id: str = Field(..., description="Frontend graph node id")
    label: Literal["Paper", "Keyword", "Author", "Journal", "Year"]
    name: str = Field(..., description="Display name")
    is_center: bool = Field(default=False, description="Whether this is the requested paper")
    properties: dict[str, Any] = Field(default_factory=dict)


class PaperGraphEdge(BaseModel):
    source: str = Field(..., description="Source graph node id")
    target: str = Field(..., description="Target graph node id")
    type: Literal["HAS_KEYWORD", "AUTHORED", "PUBLISHED_IN", "PUBLISHED_IN_YEAR"]
    properties: dict[str, Any] = Field(default_factory=dict)


class PaperNeighborGraphResponse(BaseModel):
    center: PaperGraphNode
    nodes: list[PaperGraphNode] = Field(default_factory=list)
    edges: list[PaperGraphEdge] = Field(default_factory=list)
