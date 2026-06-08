from __future__ import annotations

from typing import Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.paper_repository import get_paper_cards_batch
from app.schemas.paper import PaperCardResponse, PaperCardTrustBadge
from app.schemas.search import PaperSearchItem

_PAPER_TYPE_MAP = {"JAKO": "저널", "JAFO": "저널", "DIKO": "학위논문", "CFKO": "학회"}
_YEAR_CUTOFF = {"3y": 2023, "5y": 2021, "10y": 2016}


def apply_filters(
    items: list[PaperSearchItem],
    *,
    year_range: Optional[str] = None,
    paper_type: Optional[str] = None,
    kci: Optional[bool] = None,
    sci: Optional[bool] = None,
) -> list[PaperSearchItem]:
    if year_range:
        cutoff = _YEAR_CUTOFF.get(year_range)
        if cutoff:
            items = [i for i in items if i.year and i.year >= cutoff]

    if paper_type:
        items = [i for i in items if _PAPER_TYPE_MAP.get(i.db_code or "") == paper_type]

    if kci is True:
        items = [i for i in items if i.db_code == "JAKO"]
    elif kci is False:
        items = [i for i in items if i.db_code != "JAKO"]

    if sci is True:
        items = [i for i in items if i.db_code in ("SCIE", "SSCI", "AHCI")]

    return items


def apply_sort(
    items: list[PaperSearchItem],
    sort: Literal["citation", "date"] = "date",
) -> list[PaperSearchItem]:
    if sort == "citation":
        return sorted(items, key=lambda x: x.credibility.citation_count or 0, reverse=True)
    return sorted(items, key=lambda x: x.year or 0, reverse=True)


def paginate(
    items: list[PaperSearchItem],
    page: int,
    size: int,
) -> tuple[list[PaperSearchItem], int]:
    total = len(items)
    offset = (page - 1) * size
    return items[offset: offset + size], total


async def build_paper_cards(
    items: list[PaperSearchItem],
    db: AsyncSession,
) -> list[PaperCardResponse]:
    """ChromaDB 결과 목록 → PaperCardResponse 목록.
    papers + journals IN 쿼리 1회로 citation_count, kci_registered, sci_indexed 보강.
    """
    paper_ids = [i.paper_id for i in items]
    db_data = await get_paper_cards_batch(db, paper_ids)

    cards = []
    for item in items:
        extra = db_data.get(item.paper_id, {})
        kci_registered: bool = extra.get("kci_registered", item.db_code == "JAKO")
        sci_indexed: bool = extra.get("sci_indexed", False)
        citation_count: Optional[int] = extra.get("citation_count")

        degree_type: Optional[str] = None
        if (item.db_code or "") == "DIKO":
            degree = extra.get("degree") or ""
            if "박사" in degree:
                degree_type = "박사학위 논문"
            elif "석사" in degree:
                degree_type = "석사학위 논문"
            else:
                degree_type = "학위논문"

        trust_badge = PaperCardTrustBadge(
            kci=kci_registered,
            sci=sci_indexed,
            citation_count=citation_count,
            degree_type=degree_type,
        )

        cards.append(PaperCardResponse(
            paper_id=item.paper_id,
            title=item.title,
            authors=[a.name for a in item.authors],
            pub_year=item.year,
            journal_name=item.journal_name,
            paper_type=_PAPER_TYPE_MAP.get(item.db_code or ""),
            abstract=item.abstract,
            keywords=item.keywords,
            doi=item.doi or None,
            kci_registered=kci_registered,
            sci_indexed=sci_indexed,
            citation_count=citation_count,
            relevance_score=item.score,
            trust_badge=trust_badge,
        ))
    return cards
