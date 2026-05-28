from __future__ import annotations
from collections import Counter
from typing import Literal
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bookmark import Bookmark, BookmarkFolder
from app.models.journal import Journal
from app.models.paper import Paper
from app.schemas.bookmark import BookmarkedPaper, BookmarkListItem, BookmarkListResponse
from app.schemas.bookmark_folder import BookmarkFolderResponse

SortOption = Literal["bookmark_latest", "bookmark_oldest", "pubyear_latest", "pubyear_oldest"]
PaperTypeFilter = Literal["all", "journal", "thesis", "conference"]


async def add_bookmark(
    db: AsyncSession,
    user_id: UUID,
    paper_id: str,
    folder_id: UUID | None = None,
) -> Bookmark:
    """북마크 추가. 이미 존재하면 기존 레코드 반환."""
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


def _build_paper(paper: Paper, journal: Journal | None) -> BookmarkedPaper:
    keywords_ko = paper.keywords_ko or []
    keywords_en = paper.keywords_en or []
    keywords = keywords_ko + [k for k in keywords_en if k not in keywords_ko] or None
    return BookmarkedPaper(
        id=paper.id,
        title=paper.title,
        authors=paper.authors or None,
        pubdate=paper.pubdate or (str(paper.pubyear) if paper.pubyear else None),
        paper_type=paper.paper_type,
        doi=paper.doi,
        citation_count=paper.citation_count or 0,
        abstract=paper.abstract,
        keywords=keywords or None,
        journal_name=journal.title if journal else None,
        kci_status=journal.kci_status if journal else None,
        sjr_quartile=journal.sjr_best_quartile if journal else None,
    )


async def get_bookmarks(
    db: AsyncSession,
    user_id: UUID,
    *,
    folder_id: UUID | None = None,
    page: int = 1,
    size: int = 20,
    sort: SortOption = "bookmark_latest",
    paper_type_filter: PaperTypeFilter = "all",
    search_query: str | None = None,
) -> BookmarkListResponse:
    """북마크 목록 조회. paper_type 무관 전체 노출."""
    where = [Bookmark.user_id == user_id]

    if folder_id is not None:
        where.append(Bookmark.folder_id == folder_id)

    if paper_type_filter == "journal":
        where.append(Paper.paper_type == "journal")
    elif paper_type_filter == "thesis":
        where.append(Paper.paper_type.in_(["doctoral_thesis", "master_thesis"]))
    elif paper_type_filter == "conference":
        where.append(Paper.paper_type == "conference")

    if search_query:
        q = f"%{search_query}%"
        where.append(or_(
            Paper.title.ilike(q),
            Paper.title_en.ilike(q),
            func.array_to_string(Paper.authors, ",").ilike(q),
            func.array_to_string(Paper.keywords_ko, ",").ilike(q),
            func.array_to_string(Paper.keywords_en, ",").ilike(q),
        ))

    sort_cols = {
        "bookmark_latest": [Bookmark.created_at.desc()],
        "bookmark_oldest": [Bookmark.created_at.asc()],
        "pubyear_latest": [Paper.pubyear.desc().nullslast(), Bookmark.created_at.desc()],
        "pubyear_oldest": [Paper.pubyear.asc().nullsfirst(), Bookmark.created_at.desc()],
    }
    order = sort_cols.get(sort, sort_cols["bookmark_latest"])

    # 검색어 있을 때 완전일치 → 접두일치 → 부분일치 우선순위 앞에 추가
    if search_query:
        relevance = case(
            (Paper.title.ilike(search_query), 0),
            (Paper.title.ilike(f"{search_query}%"), 1),
            else_=2,
        )
        order = [relevance] + order

    base_join = (
        select(Bookmark, Paper, Journal)
        .join(Paper, Bookmark.paper_id == Paper.id)
        .outerjoin(Journal, Paper.journal_id == Journal.id)
        .where(*where)
    )

    total = (
        await db.execute(
            select(func.count(Bookmark.id))
            .join(Paper, Bookmark.paper_id == Paper.id)
            .outerjoin(Journal, Paper.journal_id == Journal.id)
            .where(*where)
        )
    ).scalar_one()

    rows = (
        await db.execute(base_join.order_by(*order).offset((page - 1) * size).limit(size))
    ).all()

    items = [
        BookmarkListItem(
            bookmark_id=bm.id,
            folder_id=bm.folder_id,
            bookmarked_at=bm.created_at,
            paper=_build_paper(paper, journal),
        )
        for bm, paper, journal in rows
    ]
    return BookmarkListResponse(total=total, page=page, size=size, items=items)


async def get_folders_enriched(
    db: AsyncSession,
    user_id: UUID,
) -> list[BookmarkFolderResponse]:
    """폴더 목록 + paper_count + representative_keywords + updated_at."""
    folders_result = await db.execute(
        select(BookmarkFolder)
        .where(BookmarkFolder.user_id == user_id)
        .order_by(BookmarkFolder.created_at)
    )
    folders = list(folders_result.scalars().all())
    if not folders:
        return []

    folder_ids = [f.id for f in folders]

    # 폴더별 paper_count, updated_at (MAX bookmark created_at)
    stats_result = await db.execute(
        select(
            Bookmark.folder_id,
            func.count(Bookmark.id).label("paper_count"),
            func.max(Bookmark.created_at).label("updated_at"),
        )
        .where(Bookmark.folder_id.in_(folder_ids))
        .group_by(Bookmark.folder_id)
    )
    stats = {row.folder_id: row for row in stats_result.all()}

    # 폴더별 keywords_ko 수집 (대표 키워드 추출용)
    kw_result = await db.execute(
        select(Bookmark.folder_id, Paper.keywords_ko)
        .join(Paper, Bookmark.paper_id == Paper.id)
        .where(Bookmark.folder_id.in_(folder_ids), Paper.keywords_ko.isnot(None))
    )
    folder_keywords: dict[UUID, list[str]] = {}
    for folder_id, kws in kw_result.all():
        folder_keywords.setdefault(folder_id, []).extend(kws or [])

    result = []
    for folder in folders:
        stat = stats.get(folder.id)
        all_kws = folder_keywords.get(folder.id, [])
        top2 = [kw for kw, _ in Counter(all_kws).most_common(2)]
        result.append(BookmarkFolderResponse(
            id=folder.id,
            name=folder.name,
            created_at=folder.created_at,
            paper_count=stat.paper_count if stat else 0,
            representative_keywords=top2,
            updated_at=stat.updated_at if stat else None,
        ))
    return result
