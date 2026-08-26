from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
)


def normalize_doi(doi: str) -> str:
    """URL 형태 DOI에서 순수 DOI 부분만 추출. CrossRef 호출·매칭에 사용."""
    for prefix in _DOI_PREFIXES:
        if doi.startswith(prefix):
            return doi[len(prefix):]
    return doi


async def get_paper_by_id(db: AsyncSession, paper_id: str) -> Paper | None:
    result = await db.execute(select(Paper).where(Paper.id == paper_id))
    return result.scalar_one_or_none()


async def get_paper_with_journal(db: AsyncSession, paper_id: str) -> Paper | None:
    result = await db.execute(
        select(Paper)
        .options(joinedload(Paper.journal))
        .where(Paper.id == paper_id)
    )
    return result.scalar_one_or_none()


async def get_similar_papers(db: AsyncSession, paper_id: str):
    from app.models.journal import Journal
    from app.models.paper import PaperSimilar
    from sqlalchemy import func, or_
    result = await db.execute(
        select(PaperSimilar, Paper, Journal)
        .outerjoin(Paper, PaperSimilar.internal_paper_id == Paper.id)
        .outerjoin(
            Journal,
            or_(
                func.replace(Journal.p_issn, '-', '') == Paper.issn,
                func.replace(Journal.e_issn, '-', '') == Paper.issn,
            ),
        )
        .where(PaperSimilar.source_cn == paper_id)
    )
    return result.all()


async def get_doi_by_paper_id(db: AsyncSession, paper_id: str) -> str | None:
    """paper_id로 DOI 조회. CrossRef 호출용으로 정규화된 형태로 반환."""
    result = await db.execute(select(Paper.doi).where(Paper.id == paper_id))
    raw = result.scalar_one_or_none()
    return normalize_doi(raw) if raw else None


async def get_paper_meta(db: AsyncSession, paper_id: str) -> tuple[str | None, str | None, str | None]:
    """paper_id → (db_code, kci_art_id, doi). KCI/CrossRef 분기에 사용."""
    result = await db.execute(
        select(Paper.db_code, Paper.kci_art_id, Paper.doi).where(Paper.id == paper_id)
    )
    row = result.one_or_none()
    if not row:
        return None, None, None
    db_code, kci_art_id, doi = row
    return db_code, kci_art_id, normalize_doi(doi) if doi else None


async def paper_exists_by_doi(db: AsyncSession, doi: str) -> bool:
    """DOI로 papers 테이블 존재 여부 확인 (in_service 판별용)."""
    bare = normalize_doi(doi)
    result = await db.execute(
        select(Paper.id).where(Paper.doi.like(f"%{bare}"))
    )
    return result.scalar_one_or_none() is not None


async def get_paper_cards_batch(
    db: AsyncSession, paper_ids: list[str]
) -> dict[str, dict]:
    """paper_id 목록 → {paper_id: {citation_count, kci_registered, sci_indexed, degree}} IN 쿼리 1회.
    papers.journal_id → journals.id LEFT JOIN으로 sci_indexed 함께 조회.
    """
    from app.models.journal import Journal

    if not paper_ids:
        return {}

    result = await db.execute(
        select(
            Paper.id,
            Paper.citation_count,
            Paper.db_code,
            Paper.degree,
            Journal.sci_indexed,
            Journal.kci_indexed,
        )
        .outerjoin(Journal, Paper.journal_id == Journal.id)
        .where(Paper.id.in_(paper_ids))
    )
    return {
        row.id: {
            "citation_count": row.citation_count,
            # db_code(KCI DB 수집 여부)가 주 신호 — journals.kci_indexed는 마스터 CSV가
            # KCI 전체를 담고 있지 않아 false가 "미등재"를 뜻하지 않는다. 둘을 OR로 합쳐
            # 상세 페이지(calculate_credibility의 kci_hint)와 같은 판정을 내도록 맞춘다.
            "kci_registered": row.db_code == "JAKO" or bool(row.kci_indexed),
            "sci_indexed": bool(row.sci_indexed) if row.sci_indexed is not None else False,
            "degree": row.degree,
        }
        for row in result.fetchall()
    }


async def get_papers_by_dois_batch(
    db: AsyncSession, bare_dois: list[str]
) -> dict[str, Paper]:
    """CrossRef bare DOI 리스트로 papers 테이블 배치 조회 (IN 쿼리 1회).
    반환: {normalized_doi: Paper}"""
    if not bare_dois:
        return {}
    # DB 저장 형태(URL 포함)와 bare 형태 모두 IN에 포함
    all_forms: set[str] = set()
    for doi in bare_dois:
        all_forms.add(doi)
        for prefix in _DOI_PREFIXES:
            all_forms.add(f"{prefix}{doi}")

    result = await db.execute(select(Paper).where(Paper.doi.in_(all_forms)))
    return {normalize_doi(p.doi): p for p in result.scalars().all() if p.doi}
