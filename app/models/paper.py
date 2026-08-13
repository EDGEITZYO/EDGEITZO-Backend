from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
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
    pubdate = Column(String(10), nullable=True)
    kci_art_id = Column(String(50), nullable=True, index=True)
    paper_type = Column(String(50), nullable=True)
    citation_count = Column(Integer, default=0, nullable=False)
    journal_id = Column(
        Integer,
        ForeignKey("journals.id", ondelete="SET NULL"),
        nullable=True,
    )
    journal_name = Column(String(500), nullable=True)
    db_code = Column(String(20), nullable=True)
    source = Column(String(30), nullable=True)
    degree = Column(String(30), nullable=True)
    affiliation = Column(String(500), nullable=True)
    publisher = Column(String(500), nullable=True)
    fulltext_flag = Column(Boolean, nullable=True)
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


class PaperSimilar(Base):
    __tablename__ = "paper_similar"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_cn = Column(String(100), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    similar_cn = Column(String(100), nullable=True, index=True)
    title = Column(String(1000), nullable=True)
    author = Column(Text, nullable=True)
    pubyear = Column(Integer, nullable=True)
    issn = Column(String(50), nullable=True)
    material_type = Column(String(50), nullable=True)
    internal_paper_id = Column(String(100), ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True)


class PaperCitationExternalRef(Base):
    """인용관계 그래프의 코퍼스 밖(비-in-service) 노드용 서지정보.
    양쪽 다 papers 테이블에 있는 관계는 Neo4j CITES가 담당하고, 여긴 상대 논문이
    서비스 코퍼스 밖에 있는 경우만 저장한다(제목/저자 등 표시용 메타데이터만, 상세페이지 없음)."""
    __tablename__ = "paper_citation_external_refs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_cn = Column(String(100), ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True)
    direction = Column(String(20), nullable=False)  # "reference" | "citing"
    external_source = Column(String(20), nullable=False)  # "kci" | "openalex"
    external_id = Column(String(100), nullable=False)
    title = Column(String(1000), nullable=True)
    authors = Column(ARRAY(String(300)), nullable=True)
    journal = Column(String(500), nullable=True)
    doi = Column(String(200), nullable=True)
    pubyear = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=True)

    __table_args__ = (
        Index("ix_paper_citation_external_refs_source_direction", "source_cn", "direction"),
    )
