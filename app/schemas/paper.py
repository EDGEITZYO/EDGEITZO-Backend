from typing import Optional

from pydantic import BaseModel


class ReferenceResponse(BaseModel):
    doi: Optional[str] = None
    title: Optional[str] = None
    authors: Optional[list[str]] = None
    year: Optional[int] = None
    journal: Optional[str] = None
    in_service: bool
    paper_id: Optional[str] = None        # in_service=True 일 때 우리 DB의 paper id
    unstructured: Optional[str] = None    # CrossRef fallback 원문 인용 문자열
