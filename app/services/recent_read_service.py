from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.models.journal import Journal
from app.models.paper import Paper
from app.models.recent_read import RecentRead
from app.models.user import User
from app.schemas.recent_read import RecentReadItem, RecentReadsResponse


def _row_to_recent_read_item(row) -> RecentReadItem:
    return RecentReadItem(
        paper_id=row.paper_id,
        title=row.title,
        title_en=row.title_en,
        abstract=row.abstract,
        abstract_en=row.abstract_en,
        doi=row.doi,
        issn=row.issn,
        pubyear=row.pubyear,
        source_type=row.source_type,
        db_code=row.db_code,
        journal_name=row.journal_name,
        journal_issn=list(row.journal_issn or []),
        read_at=row.read_at,
    )


async def _fetch_recent_read_item(db: AsyncSession, recent_read_id) -> RecentReadItem:
    stmt = (
        select(
            Paper.id.label("paper_id"),
            Paper.title.label("title"),
            Paper.title_en.label("title_en"),
            Paper.abstract.label("abstract"),
            Paper.abstract_en.label("abstract_en"),
            Paper.doi.label("doi"),
            Paper.issn.label("issn"),
            Paper.pubyear.label("pubyear"),
            Paper.source_type.label("source_type"),
            Paper.db_code.label("db_code"),
            Journal.title.label("journal_name"),
            Journal.issn.label("journal_issn"),
            RecentRead.read_at.label("read_at"),
        )
        .select_from(RecentRead)
        .join(Paper, RecentRead.paper_id == Paper.id)
        .outerjoin(Journal, Paper.journal_id == Journal.id)
        .where(RecentRead.id == recent_read_id)
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="recent read not found",
        )
    return _row_to_recent_read_item(row)


async def record_recent_read_service(
    db: AsyncSession,
    current_user: User,
    paper_id: str,
) -> RecentReadItem:
    normalized_paper_id = paper_id.strip()
    if not normalized_paper_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="paper_id must not be empty",
        )

    paper = await db.get(Paper, normalized_paper_id)
    if paper is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"paper not found: {normalized_paper_id}",
        )

    existing_stmt = (
        select(RecentRead)
        .where(
            RecentRead.user_id == current_user.id,
            RecentRead.paper_id == paper.id,
        )
        .order_by(RecentRead.read_at.desc(), RecentRead.id.desc())
        .limit(1)
    )
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing is not None:
        existing.read_at = now
        existing.deleted_at = None
        recent_read = existing
    else:
        recent_read = RecentRead(
            user_id=current_user.id,
            paper_id=paper.id,
            read_at=now,
            deleted_at=None,
        )
        db.add(recent_read)

    await db.commit()
    await db.refresh(recent_read)
    return await _fetch_recent_read_item(db, recent_read.id)


async def get_recent_reads_service(
    db: AsyncSession,
    current_user: User,
    *,
    limit: int = 20,
    offset: int = 0,
) -> RecentReadsResponse:
    total_stmt = (
        select(func.count(func.distinct(RecentRead.paper_id)))
        .where(
            RecentRead.user_id == current_user.id,
            RecentRead.deleted_at.is_(None),
        )
    )
    total_count = (await db.execute(total_stmt)).scalar_one()

    ranked_recent_reads = (
        select(
            RecentRead.id.label("recent_read_id"),
            RecentRead.paper_id.label("paper_id"),
            RecentRead.read_at.label("read_at"),
            func.row_number()
            .over(
                partition_by=RecentRead.paper_id,
                order_by=(RecentRead.read_at.desc(), RecentRead.id.desc()),
            )
            .label("rn"),
        )
        .where(
            RecentRead.user_id == current_user.id,
            RecentRead.deleted_at.is_(None),
        )
        .subquery()
    )

    stmt = (
        select(
            Paper.id.label("paper_id"),
            Paper.title.label("title"),
            Paper.title_en.label("title_en"),
            Paper.abstract.label("abstract"),
            Paper.abstract_en.label("abstract_en"),
            Paper.doi.label("doi"),
            Paper.issn.label("issn"),
            Paper.pubyear.label("pubyear"),
            Paper.source_type.label("source_type"),
            Paper.db_code.label("db_code"),
            Journal.title.label("journal_name"),
            Journal.issn.label("journal_issn"),
            ranked_recent_reads.c.read_at.label("read_at"),
        )
        .select_from(ranked_recent_reads)
        .join(RecentRead, RecentRead.id == ranked_recent_reads.c.recent_read_id)
        .join(Paper, RecentRead.paper_id == Paper.id)
        .outerjoin(Journal, Paper.journal_id == Journal.id)
        .where(ranked_recent_reads.c.rn == 1)
        .order_by(ranked_recent_reads.c.read_at.desc(), ranked_recent_reads.c.recent_read_id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    return RecentReadsResponse(
        total_count=total_count,
        items=[_row_to_recent_read_item(row) for row in rows],
    )
