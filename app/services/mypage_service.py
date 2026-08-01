from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bookmark import Bookmark, BookmarkFolder
from app.models.recent_read import RecentRead
from app.models.user import User
from app.models.user_keyword_map import UserKeywordMap
from app.repositories.user_repository import update_user
from app.schemas.mypage import MypageProfile, MypageResponse, MypageSummary


def _profile_from_user(user: User) -> MypageProfile:
    return MypageProfile(
        id=user.id,
        email=user.email,
        provider=user.provider,
        name=user.name,
        gender=user.gender,
        birth_year=user.birth_year,
        role=user.role,
        research_field=user.research_field,
        purposes=user.purposes,
        purpose_custom=user.purpose_custom,
        is_profile_set=user.is_profile_set,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def get_mypage_data(db: AsyncSession, user: User) -> MypageResponse:
    bookmark_count = await db.scalar(
        select(func.count()).select_from(Bookmark).where(Bookmark.user_id == user.id)
    )
    folder_count = await db.scalar(
        select(func.count())
        .select_from(BookmarkFolder)
        .where(BookmarkFolder.user_id == user.id)
    )
    recent_read_count = await db.scalar(
        select(func.count())
        .select_from(RecentRead)
        .where(
            RecentRead.user_id == user.id,
            RecentRead.deleted_at.is_(None),
        )
    )

    return MypageResponse(
        profile=_profile_from_user(user),
        summary=MypageSummary(
            bookmark_count=bookmark_count or 0,
            bookmark_folder_count=folder_count or 0,
            recent_read_count=recent_read_count or 0,
        ),
    )


async def update_mypage_profile(
    db: AsyncSession,
    user: User,
    data: dict,
) -> MypageProfile:
    if data:
        data["is_profile_set"] = True
        user = await update_user(db, user, **data)
    return _profile_from_user(user)


async def delete_account(db: AsyncSession, user: User) -> None:
    uid = user.id
    await db.execute(delete(Bookmark).where(Bookmark.user_id == uid))
    await db.execute(delete(BookmarkFolder).where(BookmarkFolder.user_id == uid))
    await db.execute(delete(RecentRead).where(RecentRead.user_id == uid))
    await db.execute(delete(UserKeywordMap).where(UserKeywordMap.user_id == uid))
    await db.execute(delete(User).where(User.id == uid))
    await db.commit()
