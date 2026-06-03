from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


_VALID_PURPOSES = {
    "연구 주제 탐색",
    "랩미팅/발표 준비",
    "논문 작성 참고",
    "최신 트렌드 파악",
    "연구자 탐색",
}


class MypageProfile(BaseModel):
    id: UUID
    email: str
    provider: Optional[str] = None
    name: Optional[str] = None
    gender: Optional[str] = None
    age: Optional[str] = None
    role: Optional[str] = None
    research_field: Optional[str] = None
    purposes: Optional[list[str]] = None
    purpose_custom: Optional[str] = None
    is_profile_set: bool
    created_at: datetime
    updated_at: datetime


class MypageSummary(BaseModel):
    bookmark_count: int = 0
    bookmark_folder_count: int = 0
    recent_read_count: int = 0


class MypageResponse(BaseModel):
    profile: MypageProfile
    summary: MypageSummary


class MypageProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    gender: Optional[Literal["남성", "여성"]] = None
    age: Optional[str] = None
    role: Optional[
        Literal[
            "대학원 진학 준비",
            "석사과정",
            "박사과정",
            "석박통합과정",
            "교수·연구원",
            "대학생",
            "기타",
        ]
    ] = None
    purposes: Optional[list[str]] = Field(default=None)
    purpose_custom: Optional[str] = None
    research_field: Optional[str] = None

    @field_validator("gender", mode="before")
    @classmethod
    def normalize_gender(cls, v: str | None) -> str | None:
        if v == "선택 안함":
            return None
        return v

    @field_validator("purposes")
    @classmethod
    def validate_purposes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        invalid = [p for p in v if p not in _VALID_PURPOSES]
        if invalid:
            raise ValueError(f"올바르지 않은 목적입니다: {', '.join(invalid)}")
        return v
