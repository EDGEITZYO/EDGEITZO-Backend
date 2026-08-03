import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=True)
    provider = Column(String, nullable=True)
    provider_id = Column(String, nullable=True)
    name = Column(String, nullable=True)
    gender = Column(String, nullable=True)
    birth_year = Column(Integer, nullable=True)
    age = Column(String, nullable=True)
    role = Column(String, nullable=True)
    research_field = Column(String, nullable=True)
    purposes = Column(JSON, nullable=True)
    purpose_custom = Column(String, nullable=True)
    is_profile_set = Column(Boolean, default=False, nullable=False)
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
