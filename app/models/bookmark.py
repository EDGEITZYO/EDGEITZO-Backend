import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import Base


class BookmarkFolder(Base):
    __tablename__ = "bookmark_folders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(100), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User")
    bookmarks = relationship("Bookmark", back_populates="folder")


class Bookmark(Base):
    __tablename__ = "bookmarks"

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
    folder_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bookmark_folders.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User")
    paper = relationship("Paper")
    folder = relationship("BookmarkFolder", back_populates="bookmarks")

    __table_args__ = (
        UniqueConstraint("user_id", "paper_id", name="uq_bookmarks_user_paper"),
        Index("ix_bookmarks_user_folder", "user_id", "folder_id"),
    )
