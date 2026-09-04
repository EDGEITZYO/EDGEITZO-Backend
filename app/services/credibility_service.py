from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.journal import Journal
from app.schemas.search import CredibilityInfo, PaperSearchItem
from app.schemas.trust_badge import TrustBadge

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
    kci_hint: bool | None = None,
) -> CredibilityInfo:
    """kci_hint: 저널 정보와 무관하게 KCI 등재로 볼 근거(현재는 db_code == 'JAKO').

    journals.kci_indexed는 KCI 등재지 마스터 CSV(2,888행) 기준인데 이 CSV가 KCI 전체를
    담고 있지 않아, false가 "미등재"가 아니라 "CSV에 없음"인 경우가 있다(실측 7건 —
    Horticultural Science and Technology). db_code가 JAKO면 ScienceON이 KCI DB에서
    수집했다는 뜻이라 더 완전한 신호이므로, hint는 참일 때만 채택해 정보를 더하기만 한다."""
    quartile = journal.sjr_quartile if journal else None
    quartile_upper = quartile.upper() if quartile else None
    sjr_score = journal.sjr_score if journal else None
    h_index = journal.h_index if journal else None
    sci_indexed = journal.sci_indexed if journal else None
    kci_registered = journal.kci_indexed if journal else None
    if kci_hint:
        kci_registered = True
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


def calculate_thesis_credibility(
    degree: str | None,
    affiliation: str | None,
) -> CredibilityInfo:
    """DIKO 학위논문 전용 신뢰도. 점수 계산 없이 학위종류+기관 표시."""
    degree_s = (degree or "").strip()
    affil_s  = (affiliation or "").strip()

    if "박사" in degree_s:
        badge: CredibilityBadge = "medium"
        degree_label = "박사학위 논문"
    elif "석사" in degree_s:
        badge = "low"
        degree_label = "석사학위 논문"
    else:
        badge = "unknown"
        degree_label = "학위논문"

    parts = [degree_label]
    if affil_s:
        parts.append(affil_s)
    summary = " / ".join(parts)

    return CredibilityInfo(
        badge=badge,
        citation_count=None,
        citation_badge=None,
        impact_factor=None,
        impact_factor_badge=None,
        kci_registered=None,
        kci_badge=None,
        sci_indexed=None,
        sci_badge=None,
        sjr_quartile=None,
        sjr_score=None,
        h_index=None,
        summary=summary,
    )


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


def resolve_paper_type(db_code: str | None, degree: str | None = None) -> str:
    """db_code + degree → internal paper_type 코드.
    반환값: 'thesis_phd' | 'thesis_master' | 'thesis' | 'journal' | 'conference'
    """
    if db_code == "DIKO":
        if degree:
            if "박사" in degree:
                return "thesis_phd"
            if "석사" in degree:
                return "thesis_master"
        return "thesis"  # degree 정보 없는 fallback
    if db_code in ("JAKO", "JAFO"):
        return "journal"
    if db_code == "CFKO":
        return "conference"
    return "journal"  # 기타 fallback


def paper_type_label(paper_type: str) -> str:
    """internal paper_type code → 사용자 노출 레이블"""
    return {
        "thesis_phd":    "박사학위 논문",
        "thesis_master": "석사학위 논문",
        "thesis":        "학위논문",
        "journal":       "학술 저널",
        "conference":    "학술 저널",
    }.get(paper_type, "학술 저널")


def build_trust_badge(
    paper_type: str,
    *,
    journal: JournalEvidence | None = None,
    citation_count: int | None = None,
    degree: str | None = None,
    institution: str | None = None,
    full_text_available: bool | None = None,
    kci_hint: bool | None = None,
) -> TrustBadge:
    """kci_hint: calculate_credibility와 동일한 근거(db_code == 'JAKO'). journals에
    매칭되는 행이 없으면 journal이 None이라 kci가 null로 내려가는데, credibility 쪽은
    hint로 true를 주고 있어 두 필드가 어긋났다. 여기서도 참일 때만 채택한다."""
    if paper_type in ("thesis_phd", "thesis_master"):
        if paper_type == "thesis_phd":
            degree_type = "박사"
        elif paper_type == "thesis_master":
            degree_type = "석사"
        else:
            degree_type = None
        return TrustBadge(
            degree_type=degree_type,
            institution=institution,
            full_text_available=full_text_available,
        )
    kci = journal.kci_indexed if journal else None
    if kci_hint:
        kci = True
    return TrustBadge(
        kci=kci,
        sci=journal.sci_indexed if journal else None,
        citation_count=citation_count,
        if_value=journal.impact_factor if journal else None,
    )


async def find_journal_by_issn(
    db: AsyncSession,
    issn: str | None,
) -> JournalEvidence | None:
    """papers.issn (정규화된 8자리) → journals 조회. journal_id FK 사용 안 함."""
    if not issn:
        return None
    normalized = _normalize_issn(issn)
    if not normalized:
        return None
    stmt = (
        select(Journal)
        .where(or_(Journal.p_issn == normalized, Journal.e_issn == normalized))
        .order_by(
            Journal.sjr_year.desc().nullslast(),
            Journal.sjr_score.desc().nullslast(),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return _journal_to_evidence(result.scalar_one_or_none())


def _journal_sort_key(journal: Journal) -> tuple[int, float, int]:
    """저널 우선순위: sjr_year desc, sjr_score desc, h_index desc, 값 없으면 항상 꼴찌.
    세 지표 모두 음수가 없으므로 None을 -1로 두면 그 순서가 그대로 재현된다."""
    return (
        journal.sjr_year if journal.sjr_year is not None else -1,
        journal.sjr_score if journal.sjr_score is not None else -1.0,
        journal.h_index if journal.h_index is not None else -1,
    )


async def enrich_items_with_credibility(
    items: list[PaperSearchItem],
    db: AsyncSession,
) -> list[PaperSearchItem]:
    """논문 건마다 저널을 따로 조회하면 결과 건수만큼 DB 왕복이 생겨(N+1) 후보가
    많을 때 응답이 크게 느려짐 — 전체 후보의 ISSN/제목을 모아 쿼리 1번으로 후보
    저널을 가져온 뒤, 항목별 최적 매칭은 메모리에서 처리한다."""
    if not items:
        return items

    all_issns: set[str] = set()
    all_titles: set[str] = set()
    for item in items:
        all_issns.update(_split_issns(item.issn))
        title = _normalize_title(item.journal_name)
        if title:
            all_titles.add(title)

    journals: list[Journal] = []
    if all_issns or all_titles:
        conditions = []
        if all_issns:
            conditions.append(Journal.issn.overlap(list(all_issns)))
        if all_titles:
            conditions.append(func.lower(Journal.title).in_(all_titles))
        result = await db.execute(select(Journal).where(or_(*conditions)))
        journals = list(result.scalars().all())

    by_issn: dict[str, list[Journal]] = {}
    by_title: dict[str, list[Journal]] = {}
    for journal in journals:
        for value in journal.issn or []:
            by_issn.setdefault(value, []).append(journal)
        if journal.title:
            by_title.setdefault(journal.title.casefold(), []).append(journal)

    for item in items:
        candidates: list[Journal] = []
        for value in _split_issns(item.issn):
            candidates.extend(by_issn.get(value, []))
        title = _normalize_title(item.journal_name)
        if title:
            candidates.extend(by_title.get(title, []))

        best = max(candidates, key=_journal_sort_key, default=None)
        item.credibility = calculate_credibility(
            citation_count=item.credibility.citation_count,
            journal_name=item.journal_name,
            journal=_journal_to_evidence(best),
            kci_hint=item.db_code == "JAKO",
        )
    return items
