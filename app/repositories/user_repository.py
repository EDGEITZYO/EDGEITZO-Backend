from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    return result.scalar_one_or_none()


async def get_user_by_provider(db: AsyncSession, provider: str, provider_id: str) -> User | None:
    result = await db.execute(
        select(User).where(User.provider == provider, User.provider_id == provider_id)
    )
    return result.scalar_one_or_none()


async def create_user(db: AsyncSession, **kwargs: Any) -> User:
    user = User(**kwargs)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_user(db: AsyncSession, user: User, **kwargs: Any) -> User:
    for key, value in kwargs.items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return user
