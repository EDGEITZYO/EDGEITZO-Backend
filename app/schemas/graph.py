from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class GraphKeywordNode(BaseModel):
    key: str = Field(..., description="키워드 노드 고유 키. 예: ko:생명공학")
    name: str = Field(..., description="키워드 표시명")
    normalized_name: Optional[str] = Field(default=None, description="정규화된 키워드명. 없으면 null")
    lang: Optional[str] = Field(default=None, description="키워드 언어. 'ko' | 'en'")
    source_field: Optional[str] = Field(default=None, description="원본 필드명. 없으면 null")
    paper_count: int = Field(default=0, ge=0, description="연결된 논문 수")
    is_center: bool = Field(default=False, description="요청한 키워드 노드 여부. 중심 노드면 true")


class GraphKeywordEdge(BaseModel):
    source: str = Field(..., description="출발 키워드 키")
    target: str = Field(..., description="도착 키워드 키")
    paper_count: int = Field(default=0, ge=0, description="두 키워드가 함께 등장한 논문 수")
    lang_pair: Optional[str] = Field(default=None, description="연결된 두 키워드의 언어 조합. 없으면 null")


class KeywordGraphResponse(BaseModel):
    center: GraphKeywordNode
    nodes: list[GraphKeywordNode] = Field(default_factory=list)
    edges: list[GraphKeywordEdge] = Field(default_factory=list)


class GraphPaperItem(BaseModel):
    cn: str = Field(..., description="논문 고유 CN")
    db_code: Optional[str] = Field(default=None, description="ScienceON DB 코드. JAKO | JAFO | DIKO | CFKO")
    title: Optional[str] = Field(default=None, description="논문 한국어 제목. 없으면 null")
    title_en: Optional[str] = Field(default=None, description="논문 영문 제목. 없으면 null")
    abstract: Optional[str] = Field(default=None, description="한국어 초록. 없으면 null")
    abstract_en: Optional[str] = Field(default=None, description="영문 초록. 없으면 null")
    doi: Optional[str] = Field(default=None, description="DOI URL. 없으면 null")
    pubyear: Optional[int] = Field(default=None, description="발행 연도. 없으면 null")
    journal_name: Optional[str] = Field(default=None, description="학술지명. 없으면 null")
    authors: list[str] = Field(default_factory=list, description="저자명 목록")
    keywords: list[str] = Field(default_factory=list, description="키워드 표시명 목록")
    keyword_keys: list[str] = Field(default_factory=list, description="키워드 노드 키 목록")


class KeywordPapersResponse(BaseModel):
    keyword: GraphKeywordNode
    total_count: int = Field(default=0, ge=0, description="키워드에 연결된 전체 논문 수")
    items: list[GraphPaperItem] = Field(default_factory=list)


class PaperGraphNode(BaseModel):
    id: str = Field(..., description="프론트엔드 그래프 노드 ID")
    label: Literal["Paper", "Keyword", "Author", "Journal", "Year"]
    name: str = Field(..., description="노드 표시명")
    is_center: bool = Field(default=False, description="요청한 논문 노드 여부. 중심 노드면 true")
    properties: dict[str, Any] = Field(default_factory=dict)


class PaperGraphEdge(BaseModel):
    source: str = Field(..., description="출발 그래프 노드 ID")
    target: str = Field(..., description="도착 그래프 노드 ID")
    type: Literal["HAS_KEYWORD", "AUTHORED", "PUBLISHED_IN", "PUBLISHED_IN_YEAR"]
    properties: dict[str, Any] = Field(default_factory=dict)


class PaperNeighborGraphResponse(BaseModel):
    center: PaperGraphNode
    nodes: list[PaperGraphNode] = Field(default_factory=list)
    edges: list[PaperGraphEdge] = Field(default_factory=list)
