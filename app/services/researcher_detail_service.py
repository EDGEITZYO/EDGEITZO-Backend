"""연구자 상세페이지 — 프로필 / 논문 리스트 / 공저자 (명세 08-01 ~ 08-04).

논문을 두 테이블에서 합쳐 온다:
  researcher_external_papers  KCI 이력 전체 (1인 평균 19.5편)
  researcher_papers           코퍼스 논문 관계 (1인 평균 1.19편)

둘 중 하나만 보면 안 된다. 외부 테이블에는 코퍼스 논문 1,439건의 관계가 없고,
코퍼스 테이블만 보면 연구자 논문의 93.6%가 사라져 연구 흐름 자체가 성립하지 않는다.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.researcher_detail import (
    CoauthorItem,
    CoauthorListResponse,
    PaperSortKey,
    ResearcherPaperItem,
    ResearcherPaperListResponse,
    ResearcherPaperYearGroup,
    ResearcherPaperYearListResponse,
    ResearcherProfileResponse,
)
from app.services.credibility_service import paper_type_label, resolve_paper_type

logger = logging.getLogger(__name__)

# 남의 연락처를 프로필에 띄우는 값이라 근거가 확실한 등급만 내보낸다.
# inferred(이름+소속 유일 일치로 '추정')는 동명이인일 때 엉뚱한 사람 메일이 노출된다.
_EMAIL_VISIBLE_CONFIDENCE = ("confirmed", "domain_verified")

# 명세 08-02: 피인용순은 "피인용 데이터가 충분한 경우에 한해" 보조 정렬로 제공.
# 인용수가 있는 논문이 이보다 적으면 정렬 옵션 자체를 노출하지 않는다.
_CITATION_SORT_MIN_PAPERS = 3

_PROFILE_SQL = """
SELECT researcher_id, source, author_name_kor, author_name_eng,
       institution_current, author_inst_kor, institution_dept,
       coalesce(keywords, keyword) AS keywords,
       email, match_confidence,
       total_papers, article_cnt, total_citations, citation_source,
       corpus_paper_count, first_pubyear, last_pubyear
FROM researchers
WHERE researcher_id = :rid
"""

# 논문 한 편 = 한 행. 같은 논문이 두 테이블에 다 있으면 외부 행이 이긴다
# (외부 행에만 KCI 원문 URL이 있고, 서지 필드는 papers 조인으로 어차피 보강된다).
_PAPERS_SQL = """
WITH ext AS (
    SELECT e.external_id,
           e.internal_paper_id,
           e.title, e.journal, e.pubyear, e.pubmonth,
           e.citation_count, e.authors, e.keywords, e.doi, e.url
    FROM researcher_external_papers e
    WHERE e.researcher_id = :rid
),
corpus_only AS (
    SELECT p.kci_art_id AS external_id,
           p.id         AS internal_paper_id,
           p.title, p.journal_name AS journal, p.pubyear,
           CASE WHEN p.pubdate ~ '^[0-9]{4}-[0-9]{2}' THEN substr(p.pubdate, 6, 2) END AS pubmonth,
           p.citation_count,
           p.authors,
           CASE WHEN p.keywords_ko IS NOT NULL AND array_length(p.keywords_ko, 1) > 0
                THEN p.keywords_ko ELSE p.keywords_en END AS keywords,
           p.doi, NULL::varchar AS url
    FROM researcher_papers rp
    JOIN papers p ON p.id = rp.paper_id
    WHERE rp.researcher_id = :rid
      AND NOT EXISTS (SELECT 1 FROM ext WHERE ext.internal_paper_id = rp.paper_id)
),
unioned AS (
    SELECT DISTINCT ON (coalesce(internal_paper_id, external_id)) *
    FROM (SELECT * FROM ext UNION ALL SELECT * FROM corpus_only) u
    ORDER BY coalesce(internal_paper_id, external_id), internal_paper_id NULLS LAST
)
SELECT u.external_id, u.internal_paper_id, u.title, u.journal, u.pubyear, u.pubmonth,
       u.citation_count, u.authors, u.keywords, u.doi, u.url,
       p.abstract, p.db_code, p.degree, p.pubdate,
       coalesce(j.sci_indexed, jn.sci_indexed) AS sci_indexed,
       rp.author_order, rp.role
FROM unioned u
LEFT JOIN papers p   ON p.id = u.internal_paper_id
LEFT JOIN journals j ON j.id = p.journal_id
-- 코퍼스 밖 논문에는 ISSN이 없어 journal_id를 못 붙인다. 학술지명으로 잇는다(실측 매칭률 91.4%).
-- 같은 제목의 저널이 40쌍 있어 SCI 등재 쪽을 우선한다 — 배지를 놓치는 쪽보다 낫다.
LEFT JOIN LATERAL (
    SELECT jj.sci_indexed FROM journals jj
    WHERE lower(btrim(jj.title)) = lower(btrim(u.journal))
    ORDER BY jj.sci_indexed DESC NULLS LAST LIMIT 1
) jn ON u.journal IS NOT NULL
LEFT JOIN researcher_papers rp
       ON rp.researcher_id = :rid AND rp.paper_id = u.internal_paper_id
"""

# 공저자 = 같은 논문에 함께 이름이 올라간 '프로필이 있는' 연구자.
# authors 문자열 배열로 뽑으면 1인 평균 33명이 나오지만 그중 77%는 이름뿐이라
# 팝오버의 '상세 정보' 버튼이 죽는다. 셀프조인은 ID로 이어지므로 동명이인 문제도 없다.
_COAUTHORS_SQL = """
WITH pairs AS (
    SELECT b.researcher_id AS coauthor_id,
           coalesce(a.internal_paper_id, a.external_id) AS paper_key
    FROM researcher_external_papers a
    JOIN researcher_external_papers b
      ON b.external_id = a.external_id AND b.researcher_id <> a.researcher_id
    WHERE a.researcher_id = :rid
    UNION
    SELECT rb.researcher_id, ra.paper_id
    FROM researcher_papers ra
    JOIN researcher_papers rb
      ON rb.paper_id = ra.paper_id AND rb.researcher_id <> ra.researcher_id
    WHERE ra.researcher_id = :rid
),
agg AS (
    SELECT coauthor_id, count(DISTINCT paper_key) AS co_paper_count
    FROM pairs GROUP BY coauthor_id
)
SELECT agg.coauthor_id, agg.co_paper_count,
       coalesce(r.author_name_kor, r.author_name_eng) AS name,
       coalesce(r.institution_current, r.author_inst_kor) AS institution,
       r.institution_dept AS department,
       coalesce(r.keywords, r.keyword) AS keywords
FROM agg JOIN researchers r ON r.researcher_id = agg.coauthor_id
ORDER BY agg.co_paper_count DESC, name NULLS LAST
"""

# '함께 쓴 N편 보기' 필터 — 그 공저자와 공동 작성한 논문만 남긴다.
_COAUTHOR_PAPER_KEYS_SQL = """
SELECT coalesce(internal_paper_id, external_id) AS paper_key
FROM researcher_external_papers WHERE researcher_id = :cid
UNION
SELECT paper_id FROM researcher_papers WHERE researcher_id = :cid
"""


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    return [v for v in value if v]


def _published_at(pubyear: Optional[int], pubmonth: Optional[str], pubdate: Optional[str]) -> Optional[str]:
    """표시용 발행일. KCI는 일자를 주지 않는 건이 많아 'YYYY-MM'까지만 나올 수 있다.

    papers.pubdate에는 '2022.10.30'(847건)과 '2022-10-30'(742건)이 섞여 있다.
    적재 시기별로 다른 파서가 채운 흔적인데, 화면에 그대로 내보내면 카드마다 구분자가
    달라지므로 여기서 대시로 통일한다.
    """
    if pubdate and len(pubdate) >= 10:
        return pubdate[:10].replace(".", "-")
    if pubyear and pubmonth:
        return f"{pubyear}-{str(pubmonth).zfill(2)}"
    if pubyear:
        return str(pubyear)
    return None


def _paper_key(row: Any) -> str:
    return row.internal_paper_id or row.external_id


async def fetch_paper_rows(
    db: AsyncSession, researcher_id: str, *, coauthor_id: Optional[str] = None
) -> list[Any]:
    """연구자의 논문 전체 행. 최대 341편(실측)이라 전부 읽어도 부담이 없고,
    연구 흐름 그래프가 같은 목록을 재사용할 수 있다."""
    rows = (await db.execute(text(_PAPERS_SQL), {"rid": researcher_id})).all()
    if coauthor_id:
        keys = {
            r.paper_key
            for r in (await db.execute(text(_COAUTHOR_PAPER_KEYS_SQL), {"cid": coauthor_id})).all()
        }
        rows = [r for r in rows if _paper_key(r) in keys]
    return rows


def _sort_rows(rows: list[Any], sort: PaperSortKey) -> list[Any]:
    if sort == "citations":
        return sorted(
            rows,
            key=lambda r: (
                r.citation_count if r.citation_count is not None else -1,
                r.pubyear or 0,
                int(r.pubmonth) if r.pubmonth and str(r.pubmonth).isdigit() else 0,
            ),
            reverse=True,
        )
    # 기본: 최신순(발행연도 내림차순, 같은 해는 날짜순)
    return sorted(
        rows,
        key=lambda r: (
            r.pubyear or 0,
            int(r.pubmonth) if r.pubmonth and str(r.pubmonth).isdigit() else 0,
            r.title or "",
        ),
        reverse=True,
    )


async def _decorate(
    db: AsyncSession, rows: list[Any], user_id: Optional[UUID]
) -> tuple[dict[str, bool], dict[str, str]]:
    """북마크 여부와 읽은 시각. 우리 DB에 있는 논문에만 붙는다."""
    internal_ids = [r.internal_paper_id for r in rows if r.internal_paper_id]
    if not user_id or not internal_ids:
        return {}, {}

    from app.services.bookmark_service import get_bookmarked_paper_ids

    bookmarked = await get_bookmarked_paper_ids(db, user_id, internal_ids)
    read_rows = (
        await db.execute(
            text(
                "SELECT paper_id, max(read_at) AS read_at FROM recent_reads "
                "WHERE user_id = :uid AND deleted_at IS NULL AND paper_id = ANY(:ids) "
                "GROUP BY paper_id"
            ),
            {"uid": str(user_id), "ids": internal_ids},
        )
    ).all()
    return (
        {pid: True for pid in bookmarked},
        {r.paper_id: r.read_at.isoformat() for r in read_rows if r.read_at},
    )


def _to_item(row: Any, bookmarked: dict[str, bool], reads: dict[str, str]) -> ResearcherPaperItem:
    internal_id = row.internal_paper_id
    is_internal = internal_id is not None
    label = None
    if row.db_code:
        label = paper_type_label(resolve_paper_type(row.db_code, row.degree))
    elif row.external_id:
        label = "학술 저널"  # KCI articleSearch는 학술지 논문만 반환한다

    return ResearcherPaperItem(
        paper_id=internal_id,
        external_id=row.external_id,
        title=row.title,
        journal_name=row.journal,
        pub_year=row.pubyear,
        pub_month=row.pubmonth,
        published_at=_published_at(row.pubyear, row.pubmonth, row.pubdate),
        authors=_as_list(row.authors),
        abstract=row.abstract,
        keywords=_as_list(row.keywords),
        # KCI가 0으로 주는 건과 미집계를 구분할 수 없다. 0은 배지를 숨기는 쪽이 안전하다.
        citation_count=row.citation_count if row.citation_count else None,
        paper_type=label,
        kci_registered=bool(row.external_id) or row.db_code == "JAKO",
        sci_indexed=row.sci_indexed,
        doi=row.doi,
        external_url=row.url,
        is_internal=is_internal,
        can_bookmark=is_internal,
        is_bookmarked=bool(internal_id and bookmarked.get(internal_id)),
        read_at=reads.get(internal_id) if internal_id else None,
        role=row.role,
        author_order=row.author_order,
    )


async def get_profile(db: AsyncSession, researcher_id: str) -> Optional[ResearcherProfileResponse]:
    row = (await db.execute(text(_PROFILE_SQL), {"rid": researcher_id})).first()
    if row is None:
        return None
    return ResearcherProfileResponse(
        researcher_id=row.researcher_id,
        name_kor=row.author_name_kor,
        name_eng=row.author_name_eng,
        institution=row.institution_current or row.author_inst_kor,
        department=row.institution_dept,
        keywords=_as_list(row.keywords),
        email=row.email if row.match_confidence in _EMAIL_VISIBLE_CONFIDENCE else None,
        total_papers=row.total_papers if row.total_papers is not None else row.article_cnt,
        total_citations=row.total_citations,
        citation_source=row.citation_source,
        corpus_paper_count=row.corpus_paper_count or 0,
        first_pubyear=row.first_pubyear,
        last_pubyear=row.last_pubyear,
    )


async def get_papers(
    db: AsyncSession,
    researcher_id: str,
    *,
    sort: PaperSortKey = "recent",
    page: int = 1,
    size: int = 6,
    coauthor_id: Optional[str] = None,
    user_id: Optional[UUID] = None,
) -> ResearcherPaperListResponse:
    rows = await fetch_paper_rows(db, researcher_id, coauthor_id=coauthor_id)
    citation_sort_available = (
        sum(1 for r in rows if r.citation_count) >= _CITATION_SORT_MIN_PAPERS
    )
    if sort == "citations" and not citation_sort_available:
        sort = "recent"  # 명세 08-02: 데이터가 부족하면 기본 정렬로 되돌린다

    ordered = _sort_rows(rows, sort)
    window = ordered[(page - 1) * size : page * size]
    bookmarked, reads = await _decorate(db, window, user_id)
    return ResearcherPaperListResponse(
        researcher_id=researcher_id,
        total=len(rows),
        page=page,
        size=size,
        sort=sort,
        citation_sort_available=citation_sort_available,
        items=[_to_item(r, bookmarked, reads) for r in window],
    )


async def get_papers_by_year(
    db: AsyncSession,
    researcher_id: str,
    *,
    page: int = 1,
    size: int = 20,
    coauthor_id: Optional[str] = None,
    user_id: Optional[UUID] = None,
) -> ResearcherPaperYearListResponse:
    """08-03. 08-02와 같은 데이터를 연도로 묶기만 한다 — 두 화면이 어긋나지 않도록."""
    rows = await fetch_paper_rows(db, researcher_id, coauthor_id=coauthor_id)
    ordered = _sort_rows(rows, "recent")
    window = ordered[(page - 1) * size : page * size]
    bookmarked, reads = await _decorate(db, window, user_id)

    groups: list[ResearcherPaperYearGroup] = []
    for row in window:
        item = _to_item(row, bookmarked, reads)
        if groups and groups[-1].year == row.pubyear:
            groups[-1].items.append(item)
            groups[-1].count += 1
        else:
            groups.append(ResearcherPaperYearGroup(year=row.pubyear, count=1, items=[item]))
    return ResearcherPaperYearListResponse(
        researcher_id=researcher_id, total=len(rows), page=page, size=size, groups=groups
    )


async def get_coauthors(
    db: AsyncSession, researcher_id: str, *, limit: int = 20
) -> CoauthorListResponse:
    rows = (await db.execute(text(_COAUTHORS_SQL), {"rid": researcher_id})).all()
    return CoauthorListResponse(
        researcher_id=researcher_id,
        total=len(rows),
        items=[
            CoauthorItem(
                researcher_id=r.coauthor_id,
                name=r.name,
                institution=r.institution,
                department=r.department,
                keywords=_as_list(r.keywords),
                co_paper_count=r.co_paper_count,
            )
            for r in rows[:limit]
        ],
    )
