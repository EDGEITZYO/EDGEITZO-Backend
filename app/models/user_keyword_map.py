import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import Base


class UserKeywordMap(Base):
    """사용자가 키워드맵에서 마지막으로 조회한 앵커 — 세션 재개(resume)용.
    그래프 자체는 매 요청 Neo4j에서 즉시 계산하므로 트리를 영속 저장하지 않음."""

    __tablename__ = "user_keyword_maps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    last_anchor_key = Column(String, nullable=False)
    last_anchor_name_ko = Column(String, nullable=True)
    last_anchor_name_en = Column(String, nullable=True)
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

    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_keyword_maps_user_id"),
    )
