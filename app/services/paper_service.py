from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.core.neo4j_client import get_neo4j_driver
from app.models.paper import Paper
from app.repositories.graph_repository import GraphRepository
from app.schemas.paper import (
    PaperDetailJournal,
    PaperDetailKeyword,
    PaperDetailResponse,
)
from app.services.credibility_service import calculate_credibility, find_journal_evidence


def _doi_to_original_url(doi: str | None) -> str | None:
    if not doi:
        return None

    value = doi.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return f"https://doi.org/{value}"


def _build_author_display(authors: list[str]) -> str | None:
    if not authors:
        return None
    if len(authors) == 1:
        return authors[0]
    return f"{authors[0]} 외 {len(authors) - 1}인"


def _merge_issns(*values: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return merged


async def _find_paper_row(
    db: AsyncSession,
    *,
    paper_cn: str,
    doi: str | None,
) -> Paper | None:
    conditions = [
        Paper.id == paper_cn,
        Paper.scienceon_cn == paper_cn,
    ]
    if doi:
        conditions.append(Paper.doi == doi)

    result = await db.execute(select(Paper).where(or_(*conditions)).limit(1))
    return result.scalar_one_or_none()


async def _build_credibility(
    detail: dict[str, Any],
    db: AsyncSession,
) -> tuple[Any, list[str]]:
    journal = detail.get("journal") or {}
    journal_name = journal.get("name") or detail.get("journal_name")
    issns = _merge_issns(detail.get("issn"), journal.get("issn"))
    citation_count = None

    try:
        paper_row = await _find_paper_row(
            db,
            paper_cn=detail["paper_cn"],
            doi=detail.get("doi"),
        )
        if paper_row is not None:
            citation_count = paper_row.citation_count
            issns = _merge_issns(issns, paper_row.issn)
    except Exception:
        paper_row = None

    try:
        journal_evidence = await find_journal_evidence(
            db,
            journal_name=journal_name,
            issn=";".join(issns),
        )
    except Exception:
        journal_evidence = None

    credibility = calculate_credibility(
        citation_count=citation_count,
        journal_name=journal_name,
        journal=journal_evidence,
    )
    return credibility, issns


async def get_paper_detail_service(
    paper_cn: str,
    db: AsyncSession,
) -> PaperDetailResponse:
    normalized_paper_cn = paper_cn.strip()
    if not normalized_paper_cn:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="paper_cn must not be empty",
        )

    driver = get_neo4j_driver()
    try:
        repository = GraphRepository(driver)
        detail = repository.find_paper_detail(normalized_paper_cn)
    finally:
        driver.close()

    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"paper not found: {normalized_paper_cn}",
        )

    authors = detail["authors"]
    keywords = [PaperDetailKeyword(**keyword) for keyword in detail["keywords"]]
    credibility, issns = await _build_credibility(detail, db)

    journal = detail.get("journal")
    if journal is not None:
        journal = {
            **journal,
            "issn": _merge_issns(journal.get("issn"), issns),
        }

    return PaperDetailResponse(
        paper_cn=detail["paper_cn"],
        db_code=detail.get("db_code"),
        title=detail.get("title"),
        title_en=detail.get("title_en"),
        display_title=detail.get("title") or detail.get("title_en") or detail["paper_cn"],
        abstract=detail.get("abstract"),
        abstract_en=detail.get("abstract_en"),
        display_abstract=detail.get("abstract") or detail.get("abstract_en"),
        doi=detail.get("doi"),
        original_url=_doi_to_original_url(detail.get("doi")),
        pubyear=detail.get("pubyear"),
        journal_name=detail.get("journal_name"),
        journal=PaperDetailJournal(**journal) if journal else None,
        issn=issns,
        authors=authors,
        author_display=_build_author_display(authors),
        author_count=len(authors),
        keywords=keywords,
        core_keywords=[keyword.name for keyword in keywords],
        credibility=credibility,
    )
