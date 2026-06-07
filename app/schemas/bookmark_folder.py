from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BookmarkFolderCreate(BaseModel):
    name: str = Field(..., max_length=100, description="폴더명 (최대 100자)", example="딥러닝 논문 모음")


class BookmarkFolderUpdate(BaseModel):
    name: str = Field(..., max_length=100, description="변경할 폴더명 (최대 100자)", example="머신러닝 논문 모음")


class BookmarkFolderResponse(BaseModel):
    id: UUID = Field(description="폴더 고유 ID")
    name: str = Field(description="폴더명")
    created_at: datetime = Field(description="폴더 생성 시각 (ISO8601)")
    paper_count: int = Field(0, description="폴더 내 북마크 수")
    representative_keywords: list[str] = Field(default=[], description="폴더 내 논문 키워드 상위 2개")
    updated_at: Optional[datetime] = Field(None, description="마지막 북마크 추가 시각 (ISO8601). 북마크 없으면 null")

    model_config = {"from_attributes": True}
