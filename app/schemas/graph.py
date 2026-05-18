from typing import Optional

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
