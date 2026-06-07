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
    "기타",
}


class MypageProfile(BaseModel):
    id: UUID = Field(description="유저 고유 ID")
    email: str = Field(description="이메일 주소")
    provider: Optional[str] = Field(None, description="소셜 로그인 제공자. 'kakao' | 'google' | null(이메일 가입)")
    name: Optional[str] = Field(None, description="이름")
    gender: Optional[str] = Field(None, description="성별. '남성' | '여성' | null")
    age: Optional[str] = Field(None, description="연령대", example="20대")
    role: Optional[str] = Field(None, description="역할. '대학원 진학 준비' | '석사과정' | '박사과정' | '석박통합과정' | '교수·연구원' | '대학생' | '기타' | null")
    research_field: Optional[str] = Field(None, description="연구 분야", example="딥러닝")
    purposes: Optional[list[str]] = Field(None, description="사용 목적 목록. '연구 주제 탐색' | '랩미팅/발표 준비' | '논문 작성 참고' | '최신 트렌드 파악' | '연구자 탐색'")
    purpose_custom: Optional[str] = Field(None, description="기타 사용 목적 직접 입력값")
    is_profile_set: bool = Field(description="프로필 최초 설정 완료 여부. false 시 온보딩 필요")
    created_at: datetime = Field(description="계정 생성 시각 (ISO8601)")
    updated_at: datetime = Field(description="프로필 마지막 수정 시각 (ISO8601)")


class MypageSummary(BaseModel):
    bookmark_count: int = Field(0, description="전체 북마크 수")
    bookmark_folder_count: int = Field(0, description="북마크 폴더 수")
    recent_read_count: int = Field(0, description="최근 열람 논문 수")


class MypageResponse(BaseModel):
    profile: MypageProfile = Field(description="계정 및 프로필 정보")
    summary: MypageSummary = Field(description="마이페이지 활동 요약")


class MypageProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, description="변경할 이름")
    gender: Optional[Literal["남성", "여성"]] = Field(None, description="성별. '남성' | '여성' | null('선택 안함' 전달 시 null로 저장)")
    age: Optional[str] = Field(None, description="연령대", example="20대")
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
    ] = Field(None, description="역할")
    purposes: Optional[list[str]] = Field(default=None, description="사용 목적 목록. 유효값: '연구 주제 탐색' | '랩미팅/발표 준비' | '논문 작성 참고' | '최신 트렌드 파악' | '연구자 탐색'")
    purpose_custom: Optional[str] = Field(None, description="기타 사용 목적 직접 입력값")
    research_field: Optional[str] = Field(None, description="연구 분야", example="딥러닝")

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
