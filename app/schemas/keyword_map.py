from typing import Literal, Optional

from pydantic import BaseModel, Field


class KeywordMapNode(BaseModel):
    key: str = Field(..., description="키워드 노드 고유 키. 예: ko:생명공학")
    name_ko: Optional[str] = Field(default=None, description="한글 표시명. 없으면 null")
    name_en: Optional[str] = Field(default=None, description="영문 표시명. 없으면 null")
    tier: int = Field(..., ge=0, description="anchor=0, 1단계 부모/자녀=1, 2단계 자녀=2, 3단계 자녀(expand로만 생성)=3")
    side: Literal["anchor", "parent", "child"] = Field(..., description="anchor 기준 좌(parent)/우(child)/중심")
    paper_count: int = Field(..., ge=0, description="이 키워드 고유 빈도 (연결된 논문 수)")
    is_hub: bool = Field(default=False, description="cross-link 연결 수가 임계값 이상인 노드 여부")
    cross_link_count: int = Field(default=0, ge=0, description="이 노드에 연결된 cross-link 엣지 수")


class KeywordMapEdge(BaseModel):
    source: str = Field(..., description="출발 키워드 키")
    target: str = Field(..., description="도착 키워드 키")
    type: Literal["tree", "cross_link"] = Field(..., description="계층 엣지(tree) 또는 교차연결 엣지(cross_link)")
    paper_count: int = Field(..., ge=0, description="두 키워드 동시출현 논문 수 (유사도 신호)")


class KeywordMapGraphResponse(BaseModel):
    anchor: KeywordMapNode
    nodes: list[KeywordMapNode] = Field(default_factory=list)
    edges: list[KeywordMapEdge] = Field(default_factory=list)
    has_more_parents: bool = Field(..., description="후보군 내 anchor보다 빈도 높은 후보 존재 여부 (좌측 화살표 활성화)")
    has_more_children: bool = Field(..., description="후보군 내 anchor보다 빈도 낮은 후보 존재 여부 (우측 화살표 노출)")


class KeywordMapExpandRequest(BaseModel):
    existing_node_keys: list[str] = Field(..., description="현재 화면에 표시 중인 모든 노드의 key (전역 dedup용)")
    current_tier: int = Field(..., ge=0, description="확장 대상 노드의 현재 tier (신규 노드는 이 값+1로 배정)")


class KeywordMapExpandResponse(BaseModel):
    parent_key: str
    new_nodes: list[KeywordMapNode] = Field(default_factory=list)
    new_edges: list[KeywordMapEdge] = Field(default_factory=list, description="tree + cross_link 모두 포함")


class KeywordMapRecenterRequest(BaseModel):
    existing_node_keys: list[str] = Field(..., description="recenter 이전 화면에 표시 중이던 모든 노드의 key")


class KeywordDefinitionOut(BaseModel):
    keyword_key: str
    definition: str
    source_url: Optional[str] = Field(default=None, description="ScienceON TREND 정의 출처 URL. LLM 생성인 경우 null")
    source: Literal["trend", "llm"] = Field(..., description="정의 문장의 출처")


class ResearcherOut(BaseModel):
    cn: str
    name_ko: Optional[str] = None
    name_en: Optional[str] = None
    institution_ko: Optional[str] = None
    article_count: Optional[int] = None


class KeywordMapNodeDetailResponse(BaseModel):
    definition: Optional[KeywordDefinitionOut] = Field(default=None, description="정의 정보 없으면 null")
    researchers: list[ResearcherOut] = Field(default_factory=list, description="데이터 미적재 시 빈 리스트")
