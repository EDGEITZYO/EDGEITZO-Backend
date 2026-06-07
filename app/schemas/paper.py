from typing import Optional

from pydantic import BaseModel, Field


class ReferenceResponse(BaseModel):
    doi: Optional[str] = Field(None, description="참고문헌 DOI. 없으면 null", example="10.1234/example")
    title: Optional[str] = Field(None, description="참고문헌 제목. 없으면 null")
    authors: Optional[list[str]] = Field(None, description="저자 목록. 없으면 null", example=["홍길동", "김철수"])
    year: Optional[int] = Field(None, description="발행 연도. 없으면 null", example=2023)
    journal: Optional[str] = Field(None, description="학술지명. 없으면 null")
    in_service: bool = Field(description="서비스 DB(papers 테이블) 수록 여부. true 시 paper_id 사용 가능")
    paper_id: Optional[str] = Field(None, description="in_service=true일 때 서비스 DB의 논문 ID. false 시 null")
    unstructured: Optional[str] = Field(None, description="CrossRef fallback 시 원문 인용 문자열. ScienceON 논문은 null")
