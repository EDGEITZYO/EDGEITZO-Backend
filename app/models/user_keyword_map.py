import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base


class UserKeywordMap(Base):
    __tablename__ = "user_keyword_maps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # TODO: user_id FK to users.id 추가 예정 (인증 연동 후)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    research_field = Column(String, nullable=False)
    tree = Column(JSON, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_keyword_maps_user_id"),
    )
