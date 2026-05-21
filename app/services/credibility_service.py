from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.journal import Journal
from app.schemas.search import CredibilityInfo, PaperSearchItem

CredibilityBadge = Literal["high", "medium", "low", "unknown"]


@dataclass(frozen=True)
class JournalEvidence:
    title: str | None = None
    sjr_quartile: str | None = None
    sjr_score: float | None = None
    h_index: int | None = None
    sci_indexed: bool | None = None
    kci_indexed: bool | None = None
    impact_factor: float | None = None


def _normalize_issn(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(ch for ch in value if ch.isdigit() or ch.upper() == "X").upper() or None


def _split_issns(value: str | None) -> list[str]:
    if not value:
        return []

    candidates = []
    for raw_part in value.replace(",", ";").replace(" ", ";").split(";"):
        raw_part = raw_part.strip().upper()
        if raw_part:
            candidates.append(raw_part)
        normalized = _normalize_issn(raw_part)
        if normalized:
            candidates.append(normalized)
            if len(normalized) == 8:
                candidates.append(f"{normalized[:4]}-{normalized[4:]}")
    return sorted(set(candidates))


def _normalize_title(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.casefold().split()) or None


def _indexed_badge(prefix: str, value: bool | None) -> str:
    if value is True:
        return f"{prefix} O"
    if value is False:
        return f"{prefix} X"
    return f"{prefix} unknown"


def _format_decimal_badge(prefix: str, value: float | None) -> str | None:
    if value is None:
        return None
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{prefix} {text}"


def _citation_badge(citation_count: int | None) -> str | None:
    if citation_count is None:
        return None
    return f"Citations {citation_count}"


def calculate_credibility(
    *,
    citation_count: int | None = None,
    journal_name: str | None = None,
    journal: JournalEvidence | None = None,
) -> CredibilityInfo:
    quartile = journal.sjr_quartile if journal else None
    quartile_upper = quartile.upper() if quartile else None
    sjr_score = journal.sjr_score if journal else None
    h_index = journal.h_index if journal else None
    sci_indexed = journal.sci_indexed if journal else None
    kci_registered = journal.kci_indexed if journal else None
    impact_factor = journal.impact_factor if journal else None

    badge: CredibilityBadge = "unknown"
    reasons: list[str] = []

    if citation_count is not None:
        reasons.append(f"citation_count={citation_count}")
        if citation_count >= 50:
            badge = "high"
        elif citation_count >= 10:
            badge = "medium"
        else:
            badge = "low"

    if quartile_upper in {"Q1", "Q2"}:
        badge = "high"
        reasons.append(f"SJR {quartile_upper}")
    elif quartile_upper in {"Q3", "Q4"} and badge != "high":
        badge = "medium"
        reasons.append(f"SJR {quartile_upper}")

    if (sci_indexed or kci_registered) and badge not in {"high", "medium"}:
        badge = "medium"
        if sci_indexed:
            reasons.append("SCI indexed")
        if kci_registered:
            reasons.append("KCI registered")

    if journal_name and badge == "unknown":
        badge = "medium"
        reasons.append("journal metadata present")

    if not reasons:
        summary = "No citation or journal evidence available."
    else:
        summary = " / ".join(reasons)

    return CredibilityInfo(
        badge=badge,
        citation_count=citation_count,
        citation_badge=_citation_badge(citation_count),
        impact_factor=impact_factor,
        impact_factor_badge=_format_decimal_badge("IF", impact_factor),
        kci_registered=kci_registered,
        kci_badge=_indexed_badge("KCI", kci_registered),
        sci_indexed=sci_indexed,
        sci_badge=_indexed_badge("SCI", sci_indexed),
        sjr_quartile=quartile_upper,
        sjr_score=sjr_score,
        h_index=h_index,
        summary=summary,
    )


def calculate_citation_only_credibility(citation_count: int | None) -> CredibilityInfo:
    return calculate_credibility(citation_count=citation_count)


def _journal_to_evidence(journal: Journal | None) -> JournalEvidence | None:
    if journal is None:
        return None
    return JournalEvidence(
        title=journal.title,
        sjr_quartile=journal.sjr_best_quartile,
        sjr_score=journal.sjr_score,
        h_index=journal.h_index,
        sci_indexed=journal.sci_indexed,
        kci_indexed=journal.kci_indexed,
        impact_factor=journal.if_value,
    )


async def find_journal_evidence(
    db: AsyncSession,
    *,
    journal_name: str | None = None,
    issn: str | None = None,
) -> JournalEvidence | None:
    issns = _split_issns(issn)
    normalized_title = _normalize_title(journal_name)

    conditions = []
    if issns:
        conditions.append(or_(*(Journal.issn.any(value) for value in issns)))
    if normalized_title:
        conditions.append(func.lower(Journal.title) == normalized_title)

    if not conditions:
        return None

    stmt = (
        select(Journal)
        .where(or_(*conditions))
        .order_by(
            Journal.sjr_year.desc().nullslast(),
            Journal.sjr_score.desc().nullslast(),
            Journal.h_index.desc().nullslast(),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return _journal_to_evidence(result.scalar_one_or_none())


async def enrich_items_with_credibility(
    items: list[PaperSearchItem],
    db: AsyncSession,
) -> list[PaperSearchItem]:
    for item in items:
        journal = await find_journal_evidence(
            db,
            journal_name=item.journal_name,
            issn=item.issn,
        )
        item.credibility = calculate_credibility(
            citation_count=item.credibility.citation_count,
            journal_name=item.journal_name,
            journal=journal,
        )
    return items
