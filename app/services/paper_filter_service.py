from __future__ import annotations

from typing import Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.paper_repository import get_paper_cards_batch
from app.schemas.paper import PaperCardResponse, PaperCardTrustBadge
from app.schemas.search import PaperSearchItem

# 필터값 → 허용 db_code 집합 (1차 pre-filter, degree 정보 없어도 가능)
_PAPER_TYPE_TO_DB_CODES: dict[str, set[str]] = {
    "학술 저널":    {"JAKO", "JAFO", "CFKO", "CFFO"},
    "학위논문":     {"DIKO"},
    "박사학위 논문": {"DIKO"},
    "석사학위 논문": {"DIKO"},
}

# db_code → 기본 레이블 (degree 모를 때 card_paper_type 초기값)
_DB_CODE_DEFAULT_LABEL: dict[str, str] = {
    "JAKO": "학술 저널",
    "JAFO": "학술 저널",
    "DIKO": "학위논문",
    "CFKO": "학술 저널",
    "CFFO": "학술 저널",
}

_YEAR_CUTOFF = {"3y": 2023, "5y": 2021, "10y": 2016}

PaperTypeFilter = Literal["학술 저널", "박사학위 논문", "석사학위 논문", "학위논문", "전체"]


def apply_filters(
    items: list[PaperSearchItem],
    *,
    year_range: Optional[str] = None,
    paper_type: Optional[str] = None,
    kci: Optional[bool] = None,
    sci: Optional[bool] = None,
) -> list[PaperSearchItem]:
    """ChromaDB items 1차 필터. paper_type은 db_code 기준으로만 거름 (degree 미반영).
    degree 기반 세분화("박사학위 논문"/"석사학위 논문")는 build_paper_cards 후
    apply_paper_type_postfilter()로 처리.
    """
    if year_range:
        cutoff = _YEAR_CUTOFF.get(year_range)
        if cutoff:
            items = [i for i in items if i.year and i.year >= cutoff]

    if paper_type and paper_type != "전체":
        allowed = _PAPER_TYPE_TO_DB_CODES.get(paper_type)
        if allowed is not None:
            items = [i for i in items if (i.db_code or "") in allowed]

    if kci is True:
        items = [i for i in items if i.db_code == "JAKO"]
    elif kci is False:
        items = [i for i in items if i.db_code != "JAKO"]

    if sci is True:
        items = [i for i in items if i.db_code in ("SCIE", "SSCI", "AHCI")]

    return items


def apply_paper_type_postfilter(
    cards: list[PaperCardResponse],
    paper_type: Optional[str],
) -> list[PaperCardResponse]:
    """build_paper_cards 이후 degree 반영된 paper_type으로 2차 필터.
    "학위논문"은 박사/석사/fallback 모두 포함.
    """
    if not paper_type or paper_type == "전체":
        return cards
    if paper_type == "학위논문":
        return [c for c in cards if c.paper_type in ("박사학위 논문", "석사학위 논문", "학위논문")]
    return [c for c in cards if c.paper_type == paper_type]


def apply_sort(
    items: list[PaperSearchItem],
    sort: Literal["citation", "date"] = "date",
) -> list[PaperSearchItem]:
    if sort == "citation":
        return sorted(items, key=lambda x: x.credibility.citation_count or 0, reverse=True)
    return sorted(items, key=lambda x: x.year or 0, reverse=True)


def paginate(
    items: list,
    page: int,
    size: int,
) -> tuple[list, int]:
    total = len(items)
    offset = (page - 1) * size
    return items[offset: offset + size], total


async def build_paper_cards(
    items: list[PaperSearchItem],
    db: AsyncSession,
) -> list[PaperCardResponse]:
    """ChromaDB 결과 목록 → PaperCardResponse 목록.
    papers + journals IN 쿼리 1회로 citation_count, kci_registered, sci_indexed, degree 보강.
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
        card_paper_type: Optional[str] = _DB_CODE_DEFAULT_LABEL.get(item.db_code or "")
        if (item.db_code or "") == "DIKO":
            degree = extra.get("degree") or ""
            if "박사" in degree:
                degree_type = "박사학위 논문"
                card_paper_type = "박사학위 논문"
            elif "석사" in degree:
                degree_type = "석사학위 논문"
                card_paper_type = "석사학위 논문"
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
            paper_type=card_paper_type,
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
