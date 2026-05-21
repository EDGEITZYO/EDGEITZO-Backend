from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bookmark import Bookmark


async def add_bookmark(
    db: AsyncSession,
    user_id: UUID,
    paper_id: str,
    folder_id: UUID | None = None,
) -> Bookmark:
    """북마크 추가. 이미 존재하면 기존 레코드 반환"""
    result = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.paper_id == paper_id)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    bm = Bookmark(user_id=user_id, paper_id=paper_id, folder_id=folder_id)
    db.add(bm)
    try:
        await db.commit()
        await db.refresh(bm)
    except IntegrityError:
        # 동시 요청으로 인한 race condition 방어
        await db.rollback()
        result = await db.execute(
            select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.paper_id == paper_id)
        )
        bm = result.scalar_one()
    return bm


async def remove_bookmark(db: AsyncSession, user_id: UUID, paper_id: str) -> bool:
    """북마크 삭제. 존재하지 않으면 False 반환."""
    result = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.paper_id == paper_id)
    )
    bm = result.scalar_one_or_none()
    if not bm:
        return False
    await db.delete(bm)
    await db.commit()
    return True


async def check_bookmark(db: AsyncSession, user_id: UUID, paper_id: str) -> bool:
    """북마크 여부 확인."""
    result = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.paper_id == paper_id)
    )
    return result.scalar_one_or_none() is not None
