from __future__ import annotations
from typing import Optional
from pydantic import BaseModel


class TrustBadge(BaseModel):
    # 저널 / 학술대회
    kci: Optional[bool] = None
    sci: Optional[bool] = None
    citation_count: Optional[int] = None
    if_value: Optional[float] = None
    # 학위논문
    degree_type: Optional[str] = None        # "박사" | "석사"
    institution: Optional[str] = None
    full_text_available: Optional[bool] = None
