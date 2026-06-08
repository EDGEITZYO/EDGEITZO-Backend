from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.paper import PaperCardTrustBadge


class BookmarkCreate(BaseModel):
    paper_id: str = Field(description="북마크할 논문 ID", example="JAKO202312345678")
    folder_id: Optional[UUID] = Field(None, description="저장할 폴더 ID. null 시 폴더 미지정 상태로 저장")


class BookmarkResponse(BaseModel):
    id: UUID = Field(description="북마크 고유 ID")
    paper_id: str = Field(description="북마크된 논문 ID")
    folder_id: Optional[UUID] = Field(description="소속 폴더 ID. 미지정 시 null")
    created_at: datetime = Field(description="북마크 생성 시각 (ISO8601)")

    model_config = {"from_attributes": True}


class BookmarkCheckResponse(BaseModel):
    paper_id: str = Field(description="확인한 논문 ID")
    bookmarked: bool = Field(description="북마크 여부")


class BookmarkedPaper(BaseModel):
    id: str = Field(description="논문 고유 ID")
    paper_type: Optional[str] = Field(None, description="논문 유형. '저널' | '학위논문' | '학회' | null(분류 불가)")
    title: str = Field(description="논문 제목")
    authors: Optional[list[str]] = Field(None, description="저자 목록")
    published_at: Optional[str] = Field(None, description="발행일 (ISO8601)")
    doi: Optional[str] = Field(None, description="DOI URL")
    citation_count: int = Field(0, description="인용 수")
    abstract: Optional[str] = Field(None, description="초록")
    keywords: Optional[list[str]] = Field(None, description="논문 키워드")
    journal_name: Optional[str] = Field(None, description="학술지명")
    trust_badge: Optional[PaperCardTrustBadge] = Field(None, description="신뢰도 뱃지 (kci, sci, citation_count, degree_type)")
    related_papers: list = Field(default=[], description="연관 논문 목록 (MVP 이후 제공 예정)")

    model_config = {"from_attributes": True}


class BookmarkListItem(BaseModel):
    bookmark_id: UUID = Field(description="북마크 고유 ID")
    folder_id: Optional[UUID] = Field(description="소속 폴더 ID. 미지정 시 null")
    bookmarked_at: datetime = Field(description="북마크 생성 시각 (ISO8601)")
    paper: BookmarkedPaper = Field(description="북마크된 논문 상세 정보")

    model_config = {"from_attributes": True}


class BookmarkListResponse(BaseModel):
    total: int = Field(description="필터 적용 후 전체 북마크 수 (페이지네이션 기준)")
    page: int = Field(description="현재 페이지 번호")
    size: int = Field(description="페이지당 결과 수")
    items: list[BookmarkListItem] = Field(description="북마크 목록")
