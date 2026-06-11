"""저장한 논문 페이지 — 북마크 탭 / 최근 읽은 탭."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone

_KST = timezone(timedelta(hours=9))
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import success_response
from app.models.bookmark import Bookmark, BookmarkFolder
from app.models.journal import Journal
from app.models.paper import Paper
from app.models.recent_read import RecentRead
from app.models.user import User
from app.schemas.bookmark_folder import BookmarkFolderResponse
from app.schemas.common import ApiErrorResponse, ApiResponse
from app.schemas.trust_badge import TrustBadge
from app.services.bookmark_service import (
    _JOURNAL_JOIN,
    _build_paper,
    get_folders_enriched,
)
from app.services.credibility_service import (
    _journal_to_evidence,
    build_trust_badge,
    paper_type_label,
    resolve_paper_type,
)

router = APIRouter(prefix="/saved", tags=["Saved"])


# ── 공통 논문 카드 ──────────────────────────────────────────────────────

class SavedPaperCard(BaseModel):
    paper_id: str
    paper_type: str
    journal_name: Optional[str]
    published_at: Optional[str]
    title: str
    authors: List[str]
    keywords: List[str]
    abstract: Optional[str]
    doi: Optional[str]
    trust_badge: TrustBadge
    bookmarked_at: Optional[str] = None
    viewed_at: Optional[str] = None
    view_count: Optional[int] = None


def _to_card(
    paper: Paper,
    journal: Journal | None,
    *,
    bookmarked_at: datetime | None = None,
    viewed_at: datetime | None = None,
    view_count: int | None = None,
) -> SavedPaperCard:
    ptype = resolve_paper_type(paper.db_code, paper.degree)
    j_ev = _journal_to_evidence(journal)
    trust = build_trust_badge(
        ptype,
        journal=j_ev,
        citation_count=paper.citation_count or None,
        institution=paper.affiliation or paper.publisher,
        full_text_available=paper.fulltext_flag,
    )
    if paper.pubdate:
        published_at = str(paper.pubdate).replace('.', '-')
    elif paper.pubyear:
        published_at = f"{paper.pubyear}-01-01"
    else:
        published_at = None

    return SavedPaperCard(
        paper_id=paper.id,
        paper_type=paper_type_label(ptype),
        journal_name=journal.title if journal else None,
        published_at=published_at,
        title=paper.title or "",
        authors=paper.authors or [],
        keywords=(paper.keywords_ko or [])[:5],
        abstract=paper.abstract,
        doi=paper.doi,
        trust_badge=trust,
        bookmarked_at=bookmarked_at.isoformat() if bookmarked_at else None,
        viewed_at=viewed_at.isoformat() if viewed_at else None,
        view_count=view_count,
    )


# ── 날짜 범위 헬퍼 ──────────────────────────────────────────────────────

def _period_range(
    period: str, ref_date: date
) -> tuple[datetime, datetime]:
    """period(day|week|month) + ref_date(KST 기준) → (start, end) UTC datetime."""
    if period == "day":
        start = datetime(ref_date.year, ref_date.month, ref_date.day, tzinfo=_KST)
        end = start + timedelta(days=1)
    elif period == "week":
        # ref_date가 속한 주(월~일)
        monday = ref_date - timedelta(days=ref_date.weekday())
        start = datetime(monday.year, monday.month, monday.day, tzinfo=_KST)
        end = start + timedelta(weeks=1)
    else:  # month
        start = datetime(ref_date.year, ref_date.month, 1, tzinfo=_KST)
        if ref_date.month == 12:
            end = datetime(ref_date.year + 1, 1, 1, tzinfo=_KST)
        else:
            end = datetime(ref_date.year, ref_date.month + 1, 1, tzinfo=_KST)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


# ── 북마크 탭 ──────────────────────────────────────────────────────────

class BookmarkListResponse(BaseModel):
    total: int
    page: int
    size: int
    items: List[SavedPaperCard]


@router.get(
    "/bookmarks",
    response_model=ApiResponse[BookmarkListResponse],
    responses={401: {"model": ApiErrorResponse}},
    summary="저장한 논문 — 북마크 탭",
    description=(
        "사용자가 북마크한 논문 목록을 반환합니다.\n\n"
        "**필터 파라미터**\n"
        "- `folder_id`: 폴더 UUID (미지정 시 전체)\n"
        "- `year`: `3y`|`5y`|`10y`|`all` — 발행 연도 범위\n"
        "- `type`: `journal`|`conference`|`thesis_phd`|`thesis_master` — 논문 유형\n"
        "- `sci`: `true`|`false` — SCI 등재 여부\n\n"
        "**trust_badge 구조**\n"
        "- 저널/학술대회: `kci`(bool), `sci`(bool), `citation_count`(int), `if_value`(float)\n"
        "- 학위논문: `degree_type`(박사|석사), `institution`(수여기관), `full_text_available`(bool)"
    ),
)
async def get_saved_bookmarks(
    folder_id: Optional[UUID] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    year: Optional[str] = Query(default=None, description="'3y'|'5y'|'10y'|'all'"),
    type: Optional[str] = Query(default=None, description="journal|conference|thesis_phd|thesis_master"),
    sci: Optional[bool] = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    where = [Bookmark.user_id == current_user.id]

    if folder_id is not None:
        where.append(Bookmark.folder_id == folder_id)

    if type == "journal":
        where.append(Paper.db_code.in_(["JAKO", "JAFO"]))
    elif type == "conference":
        where.append(Paper.db_code == "CFKO")
    elif type == "thesis_phd":
        where.append(Paper.db_code == "DIKO")
        where.append(Paper.degree.contains("박사"))
    elif type == "thesis_master":
        where.append(Paper.db_code == "DIKO")
        where.append(Paper.degree.contains("석사"))

    if year and year != "all":
        cutoff = {"3y": 2023, "5y": 2021, "10y": 2016}.get(year)
        if cutoff:
            where.append(Paper.pubyear >= cutoff)

    if sci is True:
        where.append(Journal.sci_indexed.is_(True))
    elif sci is False:
        where.append(or_(Journal.sci_indexed.is_(False), Journal.sci_indexed.is_(None)))

    base = (
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
        await db.execute(
            base.order_by(Bookmark.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()

    items = [_to_card(p, j, bookmarked_at=bm.created_at) for bm, p, j in rows]
    return success_response(
        data=BookmarkListResponse(total=total, page=page, size=size, items=items),
        message="ok",
    )


@router.get(
    "/bookmarks/folders",
    response_model=ApiResponse[list[BookmarkFolderResponse]],
    responses={401: {"model": ApiErrorResponse}},
    summary="저장한 논문 — 폴더 목록",
    description=(
        "사용자의 북마크 폴더 목록을 반환합니다.\n\n"
        "- `paper_count`: 폴더 내 북마크 수\n"
        "- `representative_keywords`: 폴더 내 논문 키워드 상위 2개\n"
        "- `updated_at`: 마지막 북마크 추가 시각"
    ),
)
async def get_saved_bookmark_folders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folders = await get_folders_enriched(db, current_user.id)
    return success_response(data=folders, message="ok")


# ── 최근 읽은 탭 ───────────────────────────────────────────────────────

class ChartPoint(BaseModel):
    paper_id: str
    paper_type: str
    journal_name: Optional[str]
    title: str
    authors: List[str]
    keywords: List[str]
    doi: Optional[str]
    published_year: Optional[int]
    citation_count: Optional[int]
    view_count: int
    viewed_at: datetime
    published_at: Optional[str]
    trust_badge: TrustBadge


class RecentListGroup(BaseModel):
    date: str                   # "YYYY-MM-DD"
    papers: List[SavedPaperCard]


class RecentChartResponse(BaseModel):
    chart_data: List[ChartPoint]


class RecentListResponse(BaseModel):
    groups: List[RecentListGroup]


@router.get(
    "/recent",
    response_model=ApiResponse[RecentListResponse | RecentChartResponse],
    responses={401: {"model": ApiErrorResponse}},
    summary="저장한 논문 — 최근 읽은 탭",
    description=(
        "기간 내 열람한 논문을 반환합니다.\n\n"
        "**`view` 파라미터**\n"
        "- `list` (기본): 날짜별 그룹핑 리스트. `groups[].date` + `groups[].papers`\n"
        "- `chart`: 산점도용 데이터. `chart_data[].published_year`(X축), "
        "`citation_count`(Y축), `view_count`(점 크기)\n\n"
        "**`period` 파라미터**\n"
        "- `day`: 해당 날짜 1일\n"
        "- `week`: 해당 날짜가 속한 주(월~일)\n"
        "- `month`: 해당 날짜가 속한 월 전체"
    ),
)
async def get_saved_recent(
    period: Literal["day", "week", "month"] = Query(default="week"),
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD (기본 오늘)"),
    view: Literal["chart", "list"] = Query(default="list"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ref = _parse_date(date)
    start, end = _period_range(period, ref)

    rows = (await db.execute(
        select(RecentRead, Paper, Journal)
        .join(Paper, RecentRead.paper_id == Paper.id)
        .outerjoin(Journal, _JOURNAL_JOIN)
        .where(
            RecentRead.user_id == current_user.id,
            RecentRead.deleted_at.is_(None),
            RecentRead.read_at >= start,
            RecentRead.read_at < end,
        )
        .order_by(desc(RecentRead.read_at))
    )).all()

    if view == "chart":
        chart_data = [
            ChartPoint(
                paper_id=p.id,
                paper_type=paper_type_label(resolve_paper_type(p.db_code, p.degree)),
                journal_name=j.title if j else None,
                title=p.title or "",
                authors=p.authors or [],
                keywords=(p.keywords_ko or [])[:5],
                doi=p.doi,
                published_year=p.pubyear,
                citation_count=p.citation_count or None,
                view_count=rr.view_count,
            )
            for rr, p, j in rows
        ]
        return success_response(data=RecentChartResponse(chart_data=chart_data), message="ok")

    # list view — 날짜별 그룹핑
    groups_dict: dict[str, list[SavedPaperCard]] = {}
    for rr, p, j in rows:
        day_key = rr.read_at.replace(tzinfo=timezone.utc).astimezone(_KST).strftime("%Y-%m-%d")
        card = _to_card(p, j, viewed_at=rr.read_at, view_count=rr.view_count)
        groups_dict.setdefault(day_key, []).append(card)

    groups = [
        RecentListGroup(date=day, papers=papers)
        for day, papers in sorted(groups_dict.items(), reverse=True)
    ]
    return success_response(data=RecentListResponse(groups=groups), message="ok")


class RecentStatsResponse(BaseModel):
    total_papers: int
    keyword_count: int
    top_keyword: Optional[str]
    chart_data: List[ChartPoint]


@router.get(
    "/recent/stats",
    response_model=ApiResponse[RecentStatsResponse],
    responses={401: {"model": ApiErrorResponse}},
    summary="최근 읽은 논문 통계",
    description=(
        "기간 내 열람 통계를 반환합니다.\n\n"
        "- `total_papers`: 총 탐색 논문 수\n"
        "- `keyword_count`: 고유 키워드 수\n"
        "- `top_keyword`: 가장 많이 등장한 키워드\n"
        "- `chart_data`: 산점도 전체 데이터 (`paper_id` / `paper_type` / `journal_name` / `title` / `authors` / `keywords` / `doi` / `published_year` / `citation_count` / `view_count`)"
    ),
)
async def get_recent_stats(
    period: Literal["day", "week", "month"] = Query(default="week"),
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD (기본 오늘)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ref = _parse_date(date)
    start, end = _period_range(period, ref)

    rows = (await db.execute(
        select(RecentRead, Paper, Journal)
        .join(Paper, RecentRead.paper_id == Paper.id)
        .outerjoin(Journal, _JOURNAL_JOIN)
        .where(
            RecentRead.user_id == current_user.id,
            RecentRead.deleted_at.is_(None),
            RecentRead.read_at >= start,
            RecentRead.read_at < end,
        )
    )).all()

    all_kws: list[str] = []
    chart_data: list[ChartPoint] = []
    for rr, p, j in rows:
        all_kws.extend(p.keywords_ko or [])
        ptype = resolve_paper_type(p.db_code, p.degree)
        j_ev = _journal_to_evidence(j)
        trust = build_trust_badge(
            ptype,
            journal=j_ev,
            citation_count=p.citation_count or None,
            institution=p.affiliation or p.publisher,
            full_text_available=p.fulltext_flag,
        )
        if p.pubdate:
            published_at = str(p.pubdate).replace('.', '-')
        elif p.pubyear:
            published_at = f"{p.pubyear}-01-01"
        else:
            published_at = None
        chart_data.append(ChartPoint(
            paper_id=p.id,
            paper_type=paper_type_label(ptype),
            journal_name=j.title if j else None,
            title=p.title or "",
            authors=p.authors or [],
            keywords=(p.keywords_ko or [])[:5],
            doi=p.doi,
            published_year=p.pubyear,
            citation_count=p.citation_count or None,
            view_count=rr.view_count,
            viewed_at=rr.read_at,
            published_at=published_at,
            trust_badge=trust,
        ))

    top_kw = Counter(all_kws).most_common(1)
    return success_response(
        data=RecentStatsResponse(
            total_papers=len(rows),
            keyword_count=len(set(all_kws)),
            top_keyword=top_kw[0][0] if top_kw else None,
            chart_data=chart_data,
        ),
        message="ok",
    )


def _parse_date(date_str: str | None) -> date:
    if not date_str:
        return datetime.now(_KST).date()
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return datetime.now(_KST).date()
