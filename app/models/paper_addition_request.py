import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base


class PaperAdditionRequest(Base):
    """사용자가 코퍼스 밖 논문의 서비스 편입을 요청한 기록."""

    __tablename__ = "paper_addition_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_id = Column(String(100), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "external_id",
            name="uq_paper_addition_requests_user_external",
        ),
        Index("ix_paper_addition_requests_external_id", "external_id"),
    )
