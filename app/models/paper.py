from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import relationship

from app.models.base import Base


class Paper(Base):
    __tablename__ = "papers"

    id = Column(String(100), primary_key=True)
    source_type = Column(String(20), nullable=False)
    scienceon_cn = Column(String(100), unique=True, nullable=True, index=True)
    semantic_scholar_id = Column(String(100), unique=True, nullable=True, index=True)
    doi = Column(String(200), unique=True, nullable=True, index=True)
    issn = Column(String(20), nullable=True, index=True)
    title = Column(String(1000), nullable=False)
    title_en = Column(String(1000), nullable=True)
    abstract = Column(Text, nullable=True)
    abstract_en = Column(Text, nullable=True)
    authors = Column(ARRAY(String(300)), nullable=True)
    keywords_ko = Column(ARRAY(String(200)), nullable=True)
    keywords_en = Column(ARRAY(String(200)), nullable=True)
    pubyear = Column(Integer, nullable=True, index=True)
    paper_type = Column(String(50), nullable=True)
    citation_count = Column(Integer, default=0, nullable=False)
    journal_id = Column(
        Integer,
        ForeignKey("journals.id", ondelete="SET NULL"),
        nullable=True,
    )
    db_code = Column(String(20), nullable=True)
    source = Column(String(30), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=True,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=True,
    )

    journal = relationship("Journal")

    __table_args__ = (
        Index("ix_papers_source_pubyear", "source", "pubyear"),
    )
