from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.search import CredibilityInfo
from app.schemas.trust_badge import TrustBadge


class PaperDetailResponse(BaseModel):
    paper_id: str = Field(description="논문 고유 ID", example="JAKO202312345678")
    title: str = Field(description="논문 제목")
    title_en: Optional[str] = Field(None, description="영문 제목. 없으면 null")
    authors: Optional[list[str]] = Field(None, description="저자 목록")
    abstract: Optional[str] = Field(None, description="초록")
    abstract_en: Optional[str] = Field(None, description="영문 초록. 없으면 null")
    keywords_ko: Optional[list[str]] = Field(None, description="한국어 키워드")
    keywords_en: Optional[list[str]] = Field(None, description="영문 키워드")
    published_at: Optional[str] = Field(None, description="발행일 (ISO8601). pubdate 우선, 없으면 '{year}-01-01'")
    paper_type: Optional[str] = Field(None, description="논문 유형. '저널' | '학위논문' | '학회' | null")
    journal_name: Optional[str] = Field(None, description="학술지명. 없으면 null")
    doi: Optional[str] = Field(None, description="DOI. 없으면 null")
    citation_count: Optional[int] = Field(None, description="인용 수. 없으면 null")
    degree: Optional[str] = Field(None, description="학위 구분 (학위논문만). 없으면 null")
    affiliation: Optional[str] = Field(None, description="소속 기관 (학위논문만). 없으면 null")
    fulltext_flag: Optional[bool] = Field(None, description="원문 제공 여부. 없으면 null")
    credibility: CredibilityInfo = Field(description="신뢰도 정보")
    trust_badge: TrustBadge = Field(description="신뢰도 뱃지")


class SimilarPaperResponse(BaseModel):
    title: Optional[str] = Field(None, description="유사 논문 제목. 없으면 null")
    author: Optional[str] = Field(None, description="저자. 없으면 null")
    pubyear: Optional[int] = Field(None, description="발행 연도. 없으면 null")
    material_type: Optional[str] = Field(None, description="자료 유형. 없으면 null")
    in_service: bool = Field(description="서비스 DB(papers 테이블) 수록 여부. true 시 paper_id로 이동 가능")
    paper_id: Optional[str] = Field(None, description="in_service=true일 때 서비스 DB의 논문 ID. false 시 null")


class ReferenceResponse(BaseModel):
    doi: Optional[str] = Field(None, description="참고문헌 DOI. 없으면 null", example="10.1234/example")
    title: Optional[str] = Field(None, description="참고문헌 제목. 없으면 null")
    authors: Optional[list[str]] = Field(None, description="저자 목록. 없으면 null", example=["홍길동", "김철수"])
    year: Optional[int] = Field(None, description="발행 연도. 없으면 null", example=2023)
    journal: Optional[str] = Field(None, description="학술지명. 없으면 null")
    in_service: bool = Field(description="서비스 DB(papers 테이블) 수록 여부. true 시 paper_id 사용 가능")
    paper_id: Optional[str] = Field(None, description="in_service=true일 때 서비스 DB의 논문 ID. false 시 null")
    unstructured: Optional[str] = Field(None, description="CrossRef fallback 시 원문 인용 문자열. ScienceON 논문은 null")
