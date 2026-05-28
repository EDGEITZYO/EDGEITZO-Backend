from __future__ import annotations
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bookmark import BookmarkFolder


async def get_folders(db: AsyncSession, user_id: UUID) -> list[BookmarkFolder]:
    """사용자의 폴더 목록 조회."""
    result = await db.execute(
        select(BookmarkFolder)
        .where(BookmarkFolder.user_id == user_id)
        .order_by(BookmarkFolder.created_at)
    )
    return list(result.scalars().all())


async def create_folder(db: AsyncSession, user_id: UUID, name: str) -> BookmarkFolder:
    """폴더 생성."""
    folder = BookmarkFolder(user_id=user_id, name=name)
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


async def update_folder(
    db: AsyncSession, user_id: UUID, folder_id: UUID, name: str
) -> BookmarkFolder | None:
    """폴더명 수정. 해당 폴더가 없거나 소유자가 다르면 None 반환."""
    result = await db.execute(
        select(BookmarkFolder).where(
            BookmarkFolder.id == folder_id,
            BookmarkFolder.user_id == user_id,
        )
    )
    folder = result.scalar_one_or_none()
    if not folder:
        return None
    folder.name = name
    await db.commit()
    await db.refresh(folder)
    return folder


async def delete_folder(db: AsyncSession, user_id: UUID, folder_id: UUID) -> bool:
    """폴더 삭제. 해당 폴더가 없거나 소유자가 다르면 False 반환."""
    result = await db.execute(
        select(BookmarkFolder).where(
            BookmarkFolder.id == folder_id,
            BookmarkFolder.user_id == user_id,
        )
    )
    folder = result.scalar_one_or_none()
    if not folder:
        return False
    await db.delete(folder)
    await db.commit()
    return True
