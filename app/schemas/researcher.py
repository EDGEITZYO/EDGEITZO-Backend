from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


ResearcherSearchType = Literal["name", "field"]


class ResearcherSearchItem(BaseModel):
    researcher_id: str
    source: str
    scienceon_cn: Optional[str] = None
    author_name_kor: Optional[str] = None
    author_name_eng: Optional[str] = None
    institution_current: Optional[str] = None
    institution_dept: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    total_papers: int = 0
    total_citations: Optional[int] = None
    citation_source: Optional[str] = None
    corpus_paper_count: int = 0
    first_pubyear: Optional[int] = None
    last_pubyear: Optional[int] = None
    field_paper_count: Optional[int] = Field(
        None,
        description="분야 검색일 때 검색어와 관련된 해당 연구자의 논문 수",
    )
    matched_keywords: list[str] = Field(default_factory=list)
    relevance_score: Optional[float] = Field(
        None,
        description="분야 검색 정렬용 관련도 점수",
    )


class ResearcherSearchResponse(BaseModel):
    query: str
    search_type: ResearcherSearchType
    total: int
    page: int
    size: int
    items: list[ResearcherSearchItem]


class ResearcherGraphNode(BaseModel):
    key: str
    node_type: Literal["field", "researcher"]
    label: str
    researcher_id: Optional[str] = None
    author_name_kor: Optional[str] = None
    author_name_eng: Optional[str] = None
    institution_current: Optional[str] = None
    institution_dept: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    total_citations: Optional[int] = None
    citation_source: Optional[str] = None
    field_paper_count: Optional[int] = None
    relevance_score: Optional[float] = None


class ResearcherGraphEdge(BaseModel):
    source: str
    target: str
    edge_type: Literal["field_relevance"]
    weight: float = Field(description="그래프 거리/굵기 계산에 사용할 관계 가중치")
    shared_keywords: list[str] = Field(default_factory=list)


class ResearcherGraphResponse(BaseModel):
    query: str
    total: int
    nodes: list[ResearcherGraphNode]
    edges: list[ResearcherGraphEdge]


class RecentResearcherSearchItem(BaseModel):
    query: str
    search_type: ResearcherSearchType
    searched_at: str


class RecentResearcherSearchResponse(BaseModel):
    items: list[RecentResearcherSearchItem]


class SaveRecentResearcherSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    search_type: ResearcherSearchType
