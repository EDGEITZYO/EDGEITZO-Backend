from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text

from app.models.base import Base


class KeywordDefinition(Base):
    """키워드 정의 캐시. TREND API 조회 결과(source='trend') 또는 근거 논문 기반 LLM 요약(source='llm')을
    최초 조회 시 생성해 영속 저장 — Neo4j Keyword.key는 Postgres FK 대상이 아니므로 FK 없음."""

    __tablename__ = "keyword_definitions"

    keyword_key = Column(String, primary_key=True)
    definition = Column(Text, nullable=False)
    source_url = Column(String, nullable=True)
    source = Column(String(10), nullable=False)  # "trend" | "llm"
    trend_cn = Column(String, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
