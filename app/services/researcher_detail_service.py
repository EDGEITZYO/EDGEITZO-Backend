"""연구자 상세페이지 — 프로필 / 논문 리스트 / 공저자 (명세 08-01 ~ 08-04).

논문을 두 테이블에서 합쳐 온다:
  researcher_external_papers  KCI 이력 전체 (1인 평균 19.5편)
  researcher_papers           코퍼스 논문 관계 (1인 평균 1.19편)

둘 중 하나만 보면 안 된다. 외부 테이블에는 코퍼스 논문 1,439건의 관계가 없고,
코퍼스 테이블만 보면 연구자 논문의 93.6%가 사라져 연구 흐름 자체가 성립하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import defaultdict
from types import SimpleNamespace
from typing import Any, Optional, Sequence
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.integrations.kci.researcher_client import KCIArticle, institution_root
from app.schemas.researcher_detail import (
    AuthorRoleCode,
    AuthorRoleFilter,
    CoauthoredPaperItem,
    CoauthoredPaperListResponse,
    CoauthorItem,
    CoauthorListResponse,
    PaperAuthorRole,
    PaperSortKey,
    ResearcherPaperItem,
    ResearcherPaperListResponse,
    ResearcherPaperYearGroup,
    ResearcherPaperYearListResponse,
    ResearcherProfileResponse,
)
from app.schemas.paper import PaperCardTrustBadge
from app.services.credibility_service import paper_type_label, resolve_paper_type
from app.services.domestic_paper_service import fetch_kci_paper, is_domestic_key
from app.services.year_range import resolve_year_range, year_bounds, year_in_range

logger = logging.getLogger(__name__)

# 남의 연락처를 프로필에 띄우는 값이라 근거가 확실한 등급만 내보낸다.
# inferred(이름+소속 유일 일치로 '추정')는 동명이인일 때 엉뚱한 사람 메일이 노출된다.
_EMAIL_VISIBLE_CONFIDENCE = ("confirmed", "domain_verified")

# 명세 08-02: 피인용순은 "피인용 데이터가 충분한 경우에 한해" 보조 정렬로 제공.
# 인용수가 있는 논문이 이보다 적으면 정렬 옵션 자체를 노출하지 않는다.
_CITATION_SORT_MIN_PAPERS = 3

# papers.pubdate의 일자를 적재가 지어 넣은 출처. 이 출처는 연월까지만 믿는다 — _published_at 참고.
_DAY_FILLED_SOURCES = ("kci", "kci_citation")

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
       p.abstract, p.db_code, p.degree, p.pubdate, p.source AS paper_source, p.title_en,
       coalesce(j.sci_indexed, jn.sci_indexed) AS sci_indexed,
       rp.author_order, rp.role
FROM unioned u
LEFT JOIN papers p   ON p.id = u.internal_paper_id
LEFT JOIN journals j ON j.id = p.journal_id
-- 코퍼스 밖 논문에는 ISSN이 없어 journal_id를 못 붙인다. 학술지명으로 잇는다(실측 매칭률 91.4%).
-- journal_id가 이미 있으면 건너뛴다. 이 LATERAL이 논문마다 journals를 훑어서,
-- 인덱스(ix_journals_title_norm) 없이는 341편 연구자에서 1.1초가 걸렸다.
-- 같은 제목의 저널이 40쌍 있어 SCI 등재 쪽을 우선한다 — 배지를 놓치는 쪽보다 낫다.
LEFT JOIN LATERAL (
    SELECT jj.sci_indexed FROM journals jj
    WHERE lower(btrim(jj.title)) = lower(btrim(u.journal))
    ORDER BY jj.sci_indexed DESC NULLS LAST LIMIT 1
) jn ON u.journal IS NOT NULL AND p.journal_id IS NULL
LEFT JOIN researcher_papers rp
       ON rp.researcher_id = :rid AND rp.paper_id = u.internal_paper_id
"""

# 공저자 = 같은 논문에 함께 이름이 올라간 '프로필이 있는' 연구자.
# authors 문자열 배열로 뽑으면 1인 평균 33명이 나오지만 그중 77%는 이름뿐이라
# 그 사람을 다시 조회할 수단이 없다. 셀프조인은 ID로 이어지므로 동명이인 문제도 없다.
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


def _published_at(
    pubyear: Optional[int],
    pubmonth: Optional[str],
    pubdate: Optional[str],
    paper_source: Optional[str] = None,
) -> Optional[str]:
    """표시용 발행일. KCI는 일자를 주지 않는 건이 많아 'YYYY-MM'까지만 나올 수 있다.

    papers.pubdate에는 '2022.10.30'(847건)과 '2022-10-30'(742건)이 섞여 있다.
    적재 시기별로 다른 파서가 채운 흔적이라, 응답 형식이 논문마다 달라지지 않도록
    여기서 대시로 통일한다.

    KCI 출처 논문은 pubdate를 쓰지 않는다. KCI API는 일자를 주지 않는데 적재
    (promote_researcher_papers·domestic_paper_service)가 01일을 채워 넣었다 — papers의
    KCI 출처 68,755편이 전부 01일이다(2026-09-29 실측). 01일만 보고 버리면 안 된다:
    ScienceON 코퍼스에는 원본이 일자까지 준 진짜 1일이 43편 있다. 그래서 값이 아니라 출처로 가른다.
    """
    if pubdate and len(pubdate) >= 10 and paper_source not in _DAY_FILLED_SOURCES:
        return pubdate[:10].replace(".", "-")
    if pubyear and pubmonth:
        return f"{pubyear}-{str(pubmonth).zfill(2)}"
    if pubyear:
        return str(pubyear)
    return None


def detail_id(row: Any) -> Optional[str]:
    """논문 상세·북마크에 넣을 ID. 우리 DB 논문 ID가 있으면 그것, 없으면 KCI 논문 ID.

    papers에 행이 없어도 KCI ID(ART…)면 상세 API와 북마크 API가 그 자리에서 KCI에서 받아 적재한다
    (paper.py get_paper_detail, bookmark_service). 그래서 is_internal=false라도 상세로 갈 수 있다 —
    예전에는 is_internal을 '상세 가능 여부'로 내보내 이런 논문(연구자 논문의 3.6%)을 막고 있었다.
    """
    if row.internal_paper_id:
        return row.internal_paper_id
    return row.external_id if is_domestic_key(row.external_id) else None


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


def _resolve_sort(rows: list[Any], sort: PaperSortKey) -> tuple[PaperSortKey, bool]:
    """(적용할 정렬, 피인용순 가능 여부). 명세 08-02: 피인용 데이터가 부족하면 기본 정렬로 되돌린다."""
    available = sum(1 for r in rows if r.citation_count) >= _CITATION_SORT_MIN_PAPERS
    if sort == "citations" and not available:
        return "recent", available
    return sort, available


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


def _paper_type(row: Any) -> Optional[str]:
    if row.db_code:
        return paper_type_label(resolve_paper_type(row.db_code, row.degree))
    if row.external_id:
        return "학술 저널"  # KCI articleSearch는 학술지 논문만 반환한다
    return None


def _kci_registered(row: Any) -> bool:
    return bool(row.external_id) or row.db_code == "JAKO"


def apply_filters(
    rows: list[Any],
    *,
    year: Optional[int] = None,
    year_start: Optional[int] = None,
    year_end: Optional[int] = None,
    paper_type: Optional[str] = None,
    kci: Optional[bool] = None,
    sci: Optional[bool] = None,
) -> list[Any]:
    """논문 리스트 필터. 키워드맵 논문 목록(paper_filter_service.apply_filters)과 같은 이름·의미다.
    연도는 year_start~year_end 범위(양 끝 포함, year는 예전 단일 연도), paper_type '전체'·null은
    필터 없음, kci/sci는 true·false일 때만 거른다."""
    start, end = resolve_year_range(year, year_start, year_end)
    if start is not None or end is not None:
        rows = [r for r in rows if year_in_range(r.pubyear, start, end)]
    if paper_type and paper_type != "전체":
        rows = [r for r in rows if _paper_type(r) == paper_type]
    if kci is not None:
        rows = [r for r in rows if _kci_registered(r) == kci]
    if sci is not None:
        rows = [r for r in rows if bool(r.sci_indexed) == sci]
    return rows


def _to_item(row: Any, bookmarked: dict[str, bool], reads: dict[str, str]) -> ResearcherPaperItem:
    internal_id = row.internal_paper_id
    is_internal = internal_id is not None
    label = _paper_type(row)
    open_id = detail_id(row)

    return ResearcherPaperItem(
        # 기존 논문 카드(PaperCardResponse)와 같게, 상세·북마크에 바로 넣을 수 있는 ID다.
        paper_id=open_id,
        external_id=row.external_id,
        title=row.title,
        journal_name=row.journal,
        pub_year=row.pubyear,
        pub_month=row.pubmonth,
        published_at=_published_at(row.pubyear, row.pubmonth, row.pubdate, row.paper_source),
        authors=_as_list(row.authors),
        abstract=row.abstract,
        keywords=_as_list(row.keywords),
        # 기존 논문 리스트와 같게 0은 0으로 보낸다(값이 없을 때만 null). 연구 흐름 카드도 같은 규칙이다.
        citation_count=row.citation_count,
        paper_type=label,
        kci_registered=_kci_registered(row),
        sci_indexed=row.sci_indexed,
        trust_badge=PaperCardTrustBadge(
            kci=_kci_registered(row),
            sci=row.sci_indexed,
            citation_count=row.citation_count,
            degree_type=label if label and "학위" in label else None,
        ),
        doi=row.doi,
        external_url=row.url,
        is_internal=is_internal,
        detail_id=open_id,
        can_open_detail=open_id is not None,
        can_bookmark=open_id is not None,
        is_bookmarked=bool(internal_id and bookmarked.get(internal_id)),
        read_at=reads.get(internal_id) if internal_id else None,
        role=row.role,
        author_order=row.author_order,
    )


async def resolve_researcher_id(db: AsyncSession, researcher_id: str) -> Optional[str]:
    """요청 ID → 실제 연구자 ID. 중복 등록을 합치며 없어진 ID는 대응표로 이어준다(037).

    공유된 링크·프런트 캐시에 옛 ID가 남아 있을 수 있어, 404 대신 합쳐진 연구자를 돌려준다.
    없는 ID면 None.
    """
    row = (
        await db.execute(
            text(
                "SELECT researcher_id FROM researchers WHERE researcher_id = :rid "
                "UNION ALL "
                "SELECT researcher_id FROM researcher_id_aliases WHERE alias_id = :rid "
                "LIMIT 1"
            ),
            {"rid": researcher_id},
        )
    ).first()
    return row.researcher_id if row else None


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
    year: Optional[int] = None,
    year_start: Optional[int] = None,
    year_end: Optional[int] = None,
    paper_type: Optional[str] = None,
    kci: Optional[bool] = None,
    sci: Optional[bool] = None,
) -> ResearcherPaperListResponse:
    rows = await fetch_paper_rows(db, researcher_id, coauthor_id=coauthor_id)
    year_min, year_max = year_bounds(r.pubyear for r in rows)
    rows = apply_filters(
        rows, year=year, year_start=year_start, year_end=year_end,
        paper_type=paper_type, kci=kci, sci=sci,
    )
    sort, citation_sort_available = _resolve_sort(rows, sort)

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
        year_min=year_min,
        year_max=year_max,
    )


async def get_papers_by_year(
    db: AsyncSession,
    researcher_id: str,
    *,
    page: int = 1,
    size: int = 20,
    coauthor_id: Optional[str] = None,
    user_id: Optional[UUID] = None,
    year: Optional[int] = None,
    year_start: Optional[int] = None,
    year_end: Optional[int] = None,
    paper_type: Optional[str] = None,
    kci: Optional[bool] = None,
    sci: Optional[bool] = None,
) -> ResearcherPaperYearListResponse:
    """08-03. 08-02와 같은 데이터를 연도로 묶기만 한다 — 두 화면이 어긋나지 않도록."""
    rows = await fetch_paper_rows(db, researcher_id, coauthor_id=coauthor_id)
    year_min, year_max = year_bounds(r.pubyear for r in rows)
    rows = apply_filters(
        rows, year=year, year_start=year_start, year_end=year_end,
        paper_type=paper_type, kci=kci, sci=sci,
    )
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
        researcher_id=researcher_id, total=len(rows), page=page, size=size, groups=groups,
        year_min=year_min, year_max=year_max,
    )


def _department_display(department: Optional[str], institution: Optional[str]) -> dict:
    """전공이 있으면 전공, 없으면 소속. 어느 쪽인지도 함께 돌려준다 — 전공과 소속을 한 필드에 섞지 않으려고
    원래 department는 그대로 두고 표시용 필드를 따로 둔다. 전공은 56%만 있다(2026-09-30)."""
    if department and department.strip():
        return {"department_display": department, "department_source": "department"}
    if institution and institution.strip():
        return {"department_display": institution, "department_source": "institution"}
    return {"department_display": None, "department_source": None}


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
                **_department_display(r.department, r.institution),
            )
            for r in rows[:limit]
        ],
    )


# ─────────────────────────────── 공저자 페이지 (함께 작성한 논문) ───────────────────────────────
#
# 역할 출처는 kci_article_author_roles(041) — KCI articleDetail의 두 속성을 그대로 둔 것이다.
#   author-division  '1' 제1저자 / '2' 그 외. 공저 논문 표본 200편 전부 보유
#   author-part      제1 | 교신 | 참여 | 단독. 2015년 이전 논문은 27%가 비어 온다(2026-11-06 실측)
# KCI ID가 없는 공저 논문(20,367편 중 68편)은 코퍼스 적재 때 받은 researcher_papers.role로 대신한다.

_ROLE_LABELS: dict[str, str] = {
    "first": "제1저자",
    "corresponding": "교신저자",
    "co_corresponding": "공동 교신저자",
    "participant": "참여저자",
}
# 필터 값 → 걸리는 역할. 필터의 교신저자는 공동 교신저자를 포함한다(필터는 세 가지, 카드는 네 가지).
_ROLE_FILTER_MATCH: dict[str, frozenset[str]] = {
    "first": frozenset({"first"}),
    "corresponding": frozenset({"corresponding", "co_corresponding"}),
    "participant": frozenset({"participant"}),
}
_DIVISION_FIRST = "1"
_PART_FIRST = frozenset({"제1", "단독"})
_PART_CORRESPONDING = "교신"
_PART_PARTICIPANT = "참여"

_AUTHOR_ROLES_SQL = """
SELECT art_id, author_names, author_institutions, author_divisions, author_parts
FROM kci_article_author_roles
WHERE art_id = ANY(:ids)
"""

# 저장 SQL과 행 모양(author_roles_row)은 일괄 수집(scripts/load_kci_author_roles.py)과 요청 중 보충이 같이 쓴다.
UPSERT_AUTHOR_ROLES_SQL = """
INSERT INTO kci_article_author_roles
    (art_id, author_names, author_institutions, author_divisions, author_parts)
VALUES (:art_id, :names, :insts, :divs, :parts)
ON CONFLICT (art_id) DO UPDATE SET
    author_names = EXCLUDED.author_names,
    author_institutions = EXCLUDED.author_institutions,
    author_divisions = EXCLUDED.author_divisions,
    author_parts = EXCLUDED.author_parts,
    fetched_at = now()
"""

# 역할이 아직 없는 공저 논문 — 연구자를 새로 적재하면 생긴다 — 은 요청 중에 KCI에서 받아 저장한다.
# 그래서 적재 뒤 수집 스크립트를 돌리지 않아도 처음 조회될 때 채워진다. 한 요청에서 받는 편수에 상한을 둬
# 응답 시간을 묶고, 넘는 편은 다음 요청에 받는다. articleDetail은 0.1~0.3초(domestic_paper_service 실측)라
# 동시 5개로 20편이면 약 1초다.
_ROLE_FETCH_MAX_PER_REQUEST = 20
_ROLE_FETCH_CONCURRENCY = 5


def author_roles_row(art_id: str, article: KCIArticle) -> dict[str, Any]:
    return {
        "art_id": art_id,
        "names": [a.name for a in article.authors],
        "insts": [a.institution for a in article.authors],
        "divs": [a.division for a in article.authors],
        "parts": [a.role for a in article.authors],
    }


async def _fetch_missing_roles(db: AsyncSession, rows: list[Any], known: dict[str, Any]) -> None:
    """known(art_id → 역할 행)에 없는 KCI 논문의 역할을 받아 저장하고 known에도 넣는다.
    받지 못한 논문은 domestic_paper_service가 5분간 다시 부르지 않으므로 그동안은 역할 없이 나간다."""
    missing = list(
        dict.fromkeys(
            r.external_id
            for r in _sort_rows(rows, "recent")  # 화면 앞쪽(최신) 논문부터
            if is_domestic_key(r.external_id) and r.external_id not in known
        )
    )[:_ROLE_FETCH_MAX_PER_REQUEST]
    if not missing:
        return

    gate = asyncio.Semaphore(_ROLE_FETCH_CONCURRENCY)
    async with httpx.AsyncClient(timeout=settings.paper_citation_external_fetch_timeout_seconds) as client:

        async def fetch(art_id: str) -> tuple[str, Any]:
            async with gate:
                return art_id, await fetch_kci_paper(art_id, client)

        fetched = await asyncio.gather(*(fetch(a) for a in missing))
    payload = [author_roles_row(art_id, paper.article) for art_id, paper in fetched if paper is not None]
    if not payload:
        return
    try:
        await db.execute(text(UPSERT_AUTHOR_ROLES_SQL), payload)
        await db.commit()
    except Exception:
        logger.exception("저자 역할 저장 실패 — 이번 응답에는 받은 값을 그대로 쓴다")
        await db.rollback()
    for item in payload:
        known[item["art_id"]] = SimpleNamespace(
            art_id=item["art_id"],
            author_names=item["names"],
            author_institutions=item["insts"],
            author_divisions=item["divs"],
            author_parts=item["parts"],
        )

_CORPUS_ROLES_SQL = """
SELECT paper_id, researcher_id, author_order, role
FROM researcher_papers
WHERE paper_id = ANY(:ids)
"""


def _display_name(person: ResearcherProfileResponse) -> Optional[str]:
    return person.name_kor or person.name_eng


def _name_key(name: Optional[str]) -> str:
    return re.sub(r"[\s·,.\-]", "", unicodedata.normalize("NFKC", name or "")).lower()


def _find_author(
    person: ResearcherProfileResponse, names: Sequence[str], institutions: Sequence[Optional[str]]
) -> Optional[int]:
    """저자 목록에서 이 연구자의 자리(0부터). 이름이 한 번만 나오면 그 자리, 같은 이름이 둘 이상이면
    소속 기관으로 좁힌다. 그래도 못 가르면 None — 엉뚱한 사람의 역할을 붙이는 것보다 비워 두는 게 낫다."""
    root = institution_root(person.institution)
    for candidate in (person.name_kor, person.name_eng):
        key = _name_key(candidate)
        if not key:
            continue
        hits = [i for i, name in enumerate(names) if _name_key(name) == key]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            narrowed = [i for i in hits if root and institutions[i] and root in institutions[i]]
            return narrowed[0] if len(narrowed) == 1 else None
    return None


def _roles_from(
    division: Optional[str], part: Optional[str], paper_parts: Sequence[Optional[str]]
) -> list[AuthorRoleCode]:
    """KCI 두 속성 → 표시 역할.

    - 제1저자: division '1' 또는 part 제1·단독. part가 비어 있는 옛 논문도 division으로 잡힌다
    - 교신저자: part 교신. 그 논문의 교신이 2명 이상이면 공동 교신저자
    - 참여저자: part 참여, 또는 part가 비었어도 그 논문에 교신 표시가 있으면(교신이 아님이 확정이라) 참여
    - 그 밖(논문에 교신 표시가 아예 없고 제1저자도 아님)은 교신인지 알 수 없어 빈 배열
    """
    roles: list[AuthorRoleCode] = []
    if division == _DIVISION_FIRST or part in _PART_FIRST:
        roles.append("first")
    if part == _PART_CORRESPONDING:
        multiple = sum(1 for p in paper_parts if p == _PART_CORRESPONDING) > 1
        roles.append("co_corresponding" if multiple else "corresponding")
    if not roles and (part == _PART_PARTICIPANT or _PART_CORRESPONDING in paper_parts):
        roles.append("participant")
    return roles


def _author_role(
    person: ResearcherProfileResponse, order: Optional[int], roles: list[AuthorRoleCode]
) -> PaperAuthorRole:
    return PaperAuthorRole(
        researcher_id=person.researcher_id,
        name=_display_name(person),
        author_order=order,
        roles=roles,
        role_labels=[_ROLE_LABELS[r] for r in roles],
    )


async def _pair_roles(
    db: AsyncSession,
    rows: list[Any],
    researcher: ResearcherProfileResponse,
    coauthor: ResearcherProfileResponse,
) -> dict[str, tuple[PaperAuthorRole, PaperAuthorRole, int]]:
    """논문 키 → (연구자 역할, 공저자 역할, 전체 저자 수)."""
    art_ids = [r.external_id for r in rows if r.external_id]
    kci = (
        {r.art_id: r for r in (await db.execute(text(_AUTHOR_ROLES_SQL), {"ids": art_ids})).all()}
        if art_ids
        else {}
    )
    await _fetch_missing_roles(db, rows, kci)

    def kci_row(row: Any) -> Any:
        found = kci.get(row.external_id) if row.external_id else None
        return found if found is not None and found.author_names else None

    fallback_ids = [r.internal_paper_id for r in rows if r.internal_paper_id and kci_row(r) is None]
    corpus: dict[str, dict[str, Any]] = defaultdict(dict)
    if fallback_ids:
        for r in (await db.execute(text(_CORPUS_ROLES_SQL), {"ids": fallback_ids})).all():
            corpus[r.paper_id][r.researcher_id] = r

    people = (researcher, coauthor)
    result: dict[str, tuple[PaperAuthorRole, PaperAuthorRole, int]] = {}
    for row in rows:
        found = kci_row(row)
        if found is not None:
            roles = []
            for person in people:
                idx = _find_author(person, found.author_names, found.author_institutions)
                roles.append(
                    _author_role(person, None, [])
                    if idx is None
                    else _author_role(
                        person,
                        idx + 1,
                        _roles_from(found.author_divisions[idx], found.author_parts[idx], found.author_parts),
                    )
                )
            author_count = len(found.author_names)
        else:
            linked = corpus.get(row.internal_paper_id, {}) if row.internal_paper_id else {}
            paper_parts = [r.role for r in linked.values()]
            roles = []
            for person in people:
                link = linked.get(person.researcher_id)
                roles.append(
                    _author_role(person, None, [])
                    if link is None
                    else _author_role(person, link.author_order, _roles_from(None, link.role, paper_parts))
                )
            author_count = len(_as_list(row.authors))
        result[_paper_key(row)] = (roles[0], roles[1], author_count)
    return result


def _role_matches(role: PaperAuthorRole, wanted: Optional[AuthorRoleFilter]) -> bool:
    return wanted is None or bool(_ROLE_FILTER_MATCH[wanted] & set(role.roles))


async def get_coauthored_papers(
    db: AsyncSession,
    researcher_id: str,
    coauthor_id: str,
    *,
    sort: PaperSortKey = "recent",
    page: int = 1,
    size: int = 6,
    researcher_role: Optional[AuthorRoleFilter] = None,
    coauthor_role: Optional[AuthorRoleFilter] = None,
    paper_type: Optional[str] = None,
    user_id: Optional[UUID] = None,
) -> CoauthoredPaperListResponse:
    """함께 작성한 논문 목록. 정렬·논문 유형은 연구자 논문 리스트(08-02)와 같은 함수를 쓴다."""
    researcher = await get_profile(db, researcher_id)
    coauthor = await get_profile(db, coauthor_id)
    rows = await fetch_paper_rows(db, researcher_id, coauthor_id=coauthor_id)
    rows = apply_filters(rows, paper_type=paper_type)
    roles = await _pair_roles(db, rows, researcher, coauthor)
    rows = [
        r
        for r in rows
        if _role_matches(roles[_paper_key(r)][0], researcher_role)
        and _role_matches(roles[_paper_key(r)][1], coauthor_role)
    ]
    sort, citation_sort_available = _resolve_sort(rows, sort)

    window = _sort_rows(rows, sort)[(page - 1) * size : page * size]
    bookmarked, reads = await _decorate(db, window, user_id)
    items = []
    for row in window:
        researcher_role_item, coauthor_role_item, author_count = roles[_paper_key(row)]
        items.append(
            CoauthoredPaperItem(
                **_to_item(row, bookmarked, reads).model_dump(),
                researcher_role=researcher_role_item,
                coauthor_role=coauthor_role_item,
                author_count=author_count,
                # 카드에 두 사람만 칩으로 나오고 나머지는 '외 N인'이다.
                other_author_count=max(author_count - len((researcher_role_item, coauthor_role_item)), 0),
            )
        )
    return CoauthoredPaperListResponse(
        researcher_id=researcher_id,
        coauthor_id=coauthor_id,
        total=len(rows),
        page=page,
        size=size,
        sort=sort,
        citation_sort_available=citation_sort_available,
        items=items,
    )
