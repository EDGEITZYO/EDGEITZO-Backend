from __future__ import annotations
from datetime import datetime
import re
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class EmailCheckRequest(BaseModel):
    email: EmailStr


class SendCodeRequest(BaseModel):
    email: EmailStr


class VerifyCodeRequest(BaseModel):
    email: EmailStr
    code: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    confirm_password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if (
            len(v) < 8
            or len(v) > 20
            or not re.search(r"[A-Z]", v)
            or not re.search(r"[a-z]", v)
            or not re.search(r"[!@#$%^&*()\-_=+\[\]{};:'\",.<>?/\\|`~]", v)
        ):
            raise ValueError("대문자, 소문자, 특수문자를 포함해야 합니다")
        return v

    @model_validator(mode="after")
    def passwords_match(self) -> "RegisterRequest":
        pw = getattr(self, "password", None)
        cpw = getattr(self, "confirm_password", None)
        if pw and cpw and pw != cpw:
            raise ValueError("비밀번호가 일치하지 않습니다")
        return self


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class StartResponse(BaseModel):
    authenticated: bool
    next_route: str
    user_id: Optional[str] = None


_VALID_PURPOSES = {
    "연구 주제 탐색",
    "랩미팅/발표 준비",
    "논문 작성 참고",
    "최신 트렌드 파악",
    "연구자 탐색",
    "기타",
}


class ProfileCreateRequest(BaseModel):
    name: str
    gender: Optional[Literal["남성", "여성", "기타"]] = None
    birth_year: Optional[int] = Field(None, ge=1900, description="출생연도", example=2002)
    role: Optional[Literal["대학원 진학 준비", "석사과정", "박사과정", "석박통합과정", "교수·연구원", "대학생", "기타"]] = None
    research_field: Optional[str] = None
    purposes: Optional[list[str]] = Field(
        default=None,
        examples=[["연구 주제 탐색", "최신 트렌드 파악"]],
    )
    purpose_custom: Optional[str] = None

    @field_validator("gender", mode="before")
    @classmethod
    def normalize_gender(cls, v: str | None) -> str | None:
        if v == "선택 안함":
            return None
        return v

    @field_validator("birth_year")
    @classmethod
    def validate_birth_year(cls, v: int | None) -> int | None:
        if v is not None and v > datetime.now().year:
            raise ValueError("출생연도는 현재 연도보다 클 수 없습니다")
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
