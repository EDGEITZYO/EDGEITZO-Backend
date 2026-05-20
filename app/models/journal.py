from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY

from app.models.base import Base


class Journal(Base):
    __tablename__ = "journals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sjr_sourceid = Column(String(50), unique=True, nullable=False, index=True)
    title = Column(String(500), nullable=False)
    issn = Column(ARRAY(String(20)), nullable=True)
    type = Column(String(50), nullable=True)
    sjr_score = Column(Float, nullable=True)
    sjr_best_quartile = Column(String(2), nullable=True)
    h_index = Column(Integer, nullable=True)
    sci_indexed = Column(Boolean, default=False, nullable=False)
    kci_indexed = Column(Boolean, default=False, nullable=False)
    if_value = Column(Float, nullable=True)
    categories = Column(ARRAY(String(200)), nullable=True)
    country = Column(String(100), nullable=True)
    coverage = Column(String(100), nullable=True)
    publisher = Column(String(300), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_journals_issn_gin", "issn", postgresql_using="gin"),
        Index("ix_journals_title", "title"),
    )
