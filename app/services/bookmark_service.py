from __future__ import annotations
from collections import Counter
from typing import Literal
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bookmark import Bookmark, BookmarkFolder
from app.models.journal import Journal
from app.models.paper import Paper
from app.schemas.bookmark import BookmarkedPaper, BookmarkListItem, BookmarkListResponse
from app.schemas.bookmark_folder import BookmarkFolderResponse
from app.services.credibility_service import (
    JournalEvidence,
    _journal_to_evidence,
    build_trust_badge,
    paper_type_label,
    resolve_paper_type,
)

SortOption = Literal["bookmark_latest", "bookmark_oldest", "pubyear_latest", "pubyear_oldest"]
PaperTypeFilter = Literal["all", "journal", "thesis_phd", "thesis_master", "conference"]

# papers.issn(하이픈 없음) vs journals.p_issn/e_issn(하이픈 있을 수 있음) 정규화 JOIN
_JOURNAL_JOIN = or_(
    func.replace(Journal.p_issn, '-', '') == Paper.issn,
    func.replace(Journal.e_issn, '-', '') == Paper.issn,
)


async def add_bookmark(
    db: AsyncSession,
    user_id: UUID,
    paper_id: str,
    folder_id: UUID | None = None,
) -> Bookmark:
    """북마크 추가. 이미 존재하면 기존 레코드 반환 (idempotent)."""
    stmt = (
        insert(Bookmark)
        .values(user_id=user_id, paper_id=paper_id, folder_id=folder_id)
        .on_conflict_do_nothing(constraint="uq_bookmarks_user_paper")
        .returning(Bookmark.id)
    )
    await db.execute(stmt)
    await db.commit()

    result = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.paper_id == paper_id)
    )
    return result.scalar_one()


async def remove_bookmark(db: AsyncSession, user_id: UUID, paper_id: str) -> bool:
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
    result = await db.execute(
        select(Bookmark).where(Bookmark.user_id == user_id, Bookmark.paper_id == paper_id)
    )
    return result.scalar_one_or_none() is not None


def _build_paper(paper: Paper, journal: Journal | None) -> BookmarkedPaper:
    keywords_ko = paper.keywords_ko or []
    keywords_en = paper.keywords_en or []
    keywords = keywords_ko + [k for k in keywords_en if k not in keywords_ko] or None

    j_ev: JournalEvidence | None = _journal_to_evidence(journal)
    ptype = resolve_paper_type(paper.db_code, paper.degree)

    if paper.pubdate:
        published_at = str(paper.pubdate).replace('.', '-')
    elif paper.pubyear:
        published_at = f"{paper.pubyear}-01-01"
    else:
        published_at = None

    trust = build_trust_badge(
        ptype,
        journal=j_ev,
        citation_count=paper.citation_count or None,
        institution=paper.affiliation or paper.publisher,
        full_text_available=paper.fulltext_flag,
    )

    return BookmarkedPaper(
        id=paper.id,
        paper_type=paper_type_label(ptype),
        title=paper.title,
        authors=paper.authors or None,
        published_at=published_at,
        doi=paper.doi,
        citation_count=paper.citation_count or 0,
        abstract=paper.abstract,
        keywords=keywords or None,
        journal_name=journal.title if journal else None,
        trust_badge=trust,
        related_papers=[],
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
    year: str | None = None,
    sci: bool | None = None,
    search_query: str | None = None,
) -> BookmarkListResponse:
    where = [Bookmark.user_id == user_id]

    if folder_id is not None:
        where.append(Bookmark.folder_id == folder_id)

    if paper_type_filter == "journal":
        where.append(Paper.db_code.in_(["JAKO", "JAFO"]))
    elif paper_type_filter == "thesis_phd":
        where.append(Paper.db_code == "DIKO")
        where.append(Paper.degree.contains("박사"))
    elif paper_type_filter == "thesis_master":
        where.append(Paper.db_code == "DIKO")
        where.append(Paper.degree.contains("석사"))
    elif paper_type_filter == "conference":
        where.append(Paper.db_code == "CFKO")

    if year:
        try:
            cutoff = {"3y": 2023, "5y": 2021, "10y": 2016}.get(year)
            if cutoff:
                where.append(Paper.pubyear >= cutoff)
        except Exception:
            pass

    if sci is True:
        where.append(Journal.sci_indexed.is_(True))
    elif sci is False:
        where.append(or_(Journal.sci_indexed.is_(False), Journal.sci_indexed.is_(None)))

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
        .outerjoin(Journal, _JOURNAL_JOIN)
        .where(*where)
    )

    total = (
        await db.execute(
            select(func.count(Bookmark.id))
            .join(Paper, Bookmark.paper_id == Paper.id)
            .outerjoin(Journal, _JOURNAL_JOIN)
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
    folders_result = await db.execute(
        select(BookmarkFolder)
        .where(BookmarkFolder.user_id == user_id)
        .order_by(BookmarkFolder.created_at)
    )
    folders = list(folders_result.scalars().all())
    if not folders:
        return []

    folder_ids = [f.id for f in folders]

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


async def get_folder_enriched_single(
    db: AsyncSession,
    user_id: UUID,
    folder_id: UUID,
) -> BookmarkFolderResponse | None:
    """단건 폴더 조회 — 본인 폴더가 아니거나 없으면 None 반환."""
    folder_result = await db.execute(
        select(BookmarkFolder).where(
            BookmarkFolder.id == folder_id,
            BookmarkFolder.user_id == user_id,
        )
    )
    folder = folder_result.scalar_one_or_none()
    if not folder:
        return None

    stats_result = await db.execute(
        select(
            func.count(Bookmark.id).label("paper_count"),
            func.max(Bookmark.created_at).label("updated_at"),
        ).where(Bookmark.folder_id == folder_id)
    )
    stat = stats_result.one()

    kw_result = await db.execute(
        select(Paper.keywords_ko)
        .join(Bookmark, Bookmark.paper_id == Paper.id)
        .where(Bookmark.folder_id == folder_id, Paper.keywords_ko.isnot(None))
    )
    all_kws: list[str] = []
    for (kws,) in kw_result.all():
        all_kws.extend(kws or [])
    top2 = [kw for kw, _ in Counter(all_kws).most_common(2)]

    return BookmarkFolderResponse(
        id=folder.id,
        name=folder.name,
        created_at=folder.created_at,
        paper_count=stat.paper_count or 0,
        representative_keywords=top2,
        updated_at=stat.updated_at,
    )
