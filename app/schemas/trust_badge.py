from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class TrustBadge(BaseModel):
    # 저널 / 학술대회
    kci: Optional[bool] = Field(
        None,
        description="KCI 등재 여부. true=등재, false=비등재, null=정보없음",
        example=True,
    )
    sci: Optional[bool] = Field(
        None,
        description="SCI 계열(SCIE/SSCI/AHCI) 등재 여부. 현재 데이터 미적재로 항상 false",
        example=False,
    )
    citation_count: Optional[int] = Field(
        None,
        description="인용 수. papers 테이블 기준. null이면 미집계",
        example=15,
    )
    if_value: Optional[float] = Field(
        None,
        description="Impact Factor. journals 테이블 if_value. 학위논문은 null",
        example=1.23,
    )
    # 학위논문
    degree_type: Optional[str] = Field(
        None,
        description="학위 구분. '박사' | '석사' | null (학위논문 전용, 저널/학회는 null)",
        example=None,
    )
    institution: Optional[str] = Field(
        None,
        description="수여 기관명 (학위논문 전용, 저널/학회는 null)",
        example=None,
    )
    full_text_available: Optional[bool] = Field(
        None,
        description="원문 제공 여부. papers.fulltext_flag 기준. null이면 미확인",
        example=True,
    )
