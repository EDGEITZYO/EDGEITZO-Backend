import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import Base


class RecentRead(Base):
    __tablename__ = "recent_reads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    paper_id = Column(
        String(100),
        ForeignKey("papers.id", ondelete="CASCADE"),
        nullable=False,
    )
    read_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    view_count = Column(Integer, default=1, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    paper = relationship("Paper")

    __table_args__ = (
        Index("ix_recent_reads_user_read_at", "user_id", "read_at"),
        Index("ix_recent_reads_user_deleted_at", "user_id", "deleted_at"),
    )
