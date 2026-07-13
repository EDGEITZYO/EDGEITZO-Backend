from __future__ import annotations

import logging
import random
import uuid
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.schemas.search import (
    PaperSearchItem,
    SearchPapersRequest,
    SearchPapersResponse,
)
from app.repositories.paper_repository import get_paper_cards_batch
from app.services.chroma_search_service import get_chroma_search_service
from app.services.credibility_service import enrich_items_with_credibility, paper_type_label, resolve_paper_type
from app.services.llm.client import chat

logger = logging.getLogger(__name__)

# 연도 점수 가중치
_SCORE_RECENT_HIGH = 0.25      # 최신 선호 목적, 2024년 이상
_SCORE_RECENT_MID = 0.15       # 최신 선호 목적, 2022~2023년
_SCORE_RECENT_LOW = 0.05       # 최신 선호 목적, 2020~2021년
_SCORE_YEAR_HIGH = 0.2         # 일반 목적, 2024년 이상
_SCORE_YEAR_MID = 0.1          # 일반 목적, 2021~2023년

# 인용수 점수 가중치
_SCORE_CITATION_MAX = 0.2      # 인용수 보너스 상한
_SCORE_CITATION_DIVISOR = 1000 # 인용수 보너스 분모

# 신뢰도 배지 가중치
_SCORE_BADGE_HIGH = 0.2
_SCORE_BADGE_MEDIUM = 0.1

# 키워드 수 가중치
_SCORE_KEYWORD_PER = 0.03
_SCORE_KEYWORD_MAX = 0.15


def _resolve_scope_badge(credibility) -> str | None:
    if credibility.kci_registered is True:
        return "KCI"
    if credibility.sci_indexed is True:
        return "SCI"
    return None


# C-2: HyDE 쿼리 확장 — 사용자 쿼리를 가상 논문 초록으로 변환해 임베딩 공간 정렬
# 짧은 쿼리와 긴 논문 초록 간 길이/표현 불균형 해소
async def expand_query_for_embedding(query: str) -> str:
    try:
        from app.services.llm.client import chat
        resp = await chat(
            messages=[{
                "role": "user",
                "content": (
                    f"다음 검색어에 대해 가상의 한국어 학술 논문 초록을 작성하라.\n"
                    f"검색어: \"{query}\"\n\n"
                    f"규칙:\n"
                    f"- 3~4문장의 자연스러운 논문 초록 형식\n"
                    f"- 연구 배경, 방법, 결과 흐름 포함\n"
                    f"- 실제 학술 용어 사용 (한글, 필요시 영문 병기)\n"
                    f"- 가상의 수치나 구체적 결론은 만들지 말 것\n\n"
                    f"초록만 반환 (제목, 설명, 따옴표 없이):"
                ),
            }],
            model=settings.llm_default_model,
            temperature=0.3,
            max_tokens=300,
        )
        hypothetical = resp.text.strip()
        return f"{query} {hypothetical}" if hypothetical else query
    except Exception as e:
        logger.warning("HyDE 쿼리 확장 실패, 원본 사용: %s", e)
        return query


def _apply_scoring(items: list[PaperSearchItem], research_purpose: str = "") -> list[PaperSearchItem]:
    purpose_prefers_recent = research_purpose in ("랩미팅발표", "최신트렌드")
    purpose_prefers_citation = research_purpose in ("논문작성참고", "연구주제탐색")

    for item in items:
        base = item.score  # ChromaDB RRF 점수 기반

        if item.year:
            if purpose_prefers_recent:
                if item.year >= 2024:
                    base += _SCORE_RECENT_HIGH
                elif item.year >= 2022:
                    base += _SCORE_RECENT_MID
                elif item.year >= 2020:
                    base += _SCORE_RECENT_LOW
            else:
                if item.year >= 2024:
                    base += _SCORE_YEAR_HIGH
                elif item.year >= 2021:
                    base += _SCORE_YEAR_MID

        if purpose_prefers_citation and item.credibility.citation_count:
            citation_bonus = min(item.credibility.citation_count / _SCORE_CITATION_DIVISOR, _SCORE_CITATION_MAX)
            base += citation_bonus

        if item.credibility.badge == "high":
            base += _SCORE_BADGE_HIGH
        elif item.credibility.badge == "medium":
            base += _SCORE_BADGE_MEDIUM

        base += min(len(item.keywords) * _SCORE_KEYWORD_PER, _SCORE_KEYWORD_MAX)
        item.score = round(base, 4)

    return items


def _sort_items(items: list[PaperSearchItem]) -> list[PaperSearchItem]:
    return sorted(items, key=lambda x: x.score, reverse=True)


def _apply_sort_order(items: list[PaperSearchItem], sort_order: str) -> list[PaperSearchItem]:
    """sort_order 기준 재정렬. 'relevance'는 이미 _sort_items()로 정렬된 순서를 그대로 유지."""
    if sort_order == "year_asc":
        return sorted(items, key=lambda i: (i.year is None, i.year or 0))
    if sort_order == "year_desc":
        return sorted(items, key=lambda i: (i.year is None, -(i.year or 0)))
    if sort_order == "citation_desc":
        return sorted(items, key=lambda i: i.credibility.citation_count or 0, reverse=True)
    return items


_SORT_LABELS = {
    "relevance": "관련도 기준",
    "year_desc": "최신순",
    "year_asc": "오래된순",
    "citation_desc": "인용수 높은순",
}

_SUMMARY_SYSTEM_PROMPT = """너는 논문 검색 결과를 사용자에게 소개하는 문장을 쓰는 도우미다.

반드시 지켜야 할 규칙:
1. 1~2문장으로 간결하게 쓴다.
2. 존댓말을 쓰되 딱딱하지 않게, "~해 드릴게요", "~하시면 좋을 것 같아요" 같은 부드러운 종결어미를 쓴다.
3. 차분하고 사려깊은 톤을 유지한다.
4. 아래로 전달되는 "실제 데이터"의 총 건수와 키워드는 문장에 반드시 포함한다.
5. 전달받지 않은 논문 제목, 저자, 통계, 키워드, 유형 등은 절대로 새로 만들어내지 않는다. 오직 전달받은 사실만 사용한다.
6. 매번 표현을 다르게 써서 같은 문장이 반복되지 않게 한다.
7. 결과 목록을 나열하지 말고, 검색을 어떻게 진행했는지에 대한 요약만 전달한다.
8. 문장 외의 다른 텍스트(따옴표, 설명, 마크다운 등)는 출력하지 않는다."""

_SUMMARY_FALLBACK_TEMPLATES = [
    "{topic} 관련 논문을 {total}건 정리했어요. {keywords} 키워드를 중심으로 {sort_label}으로 나열해 보여드려요.",
    "{topic}를 살펴봤어요. {keywords} 키워드로 찾은 {total}건을 {sort_label}으로 정리해 두었어요.",
    "{topic}에 관해 {total}건을 추려봤어요. {keywords} 키워드를 기준으로 {sort_label} 정렬했어요.",
]


def _build_summary_fallback(topic: str, total: int, keywords: list[str], sort_label: str) -> str:
    template = random.choice(_SUMMARY_FALLBACK_TEMPLATES)
    return template.format(
        topic=topic or "요청하신 주제",
        total=total,
        keywords=", ".join(keywords) if keywords else "입력하신",
        sort_label=sort_label,
    )


async def generate_result_summary(
    topic: str,
    total: int,
    keywords: list[str],
    type_distribution: dict[str, int],
    sort_order: str,
) -> str:
    """검색 결과 요약 문장 생성. 건수/키워드/유형분포는 코드에서 계산한 사실이며,
    LLM은 이 사실을 문장으로 풀어쓰는 역할만 한다 (숫자를 직접 세지 않음)."""
    sort_label = _SORT_LABELS.get(sort_order, "관련도 기준")
    topic = topic or " ".join(keywords) or "요청하신 주제"

    fact_lines = [
        f"주제: {topic}",
        f"총 건수: {total}건",
        f"키워드: {', '.join(keywords) if keywords else '없음'}",
        f"정렬 기준: {sort_label}",
    ]
    if type_distribution:
        dist_str = ", ".join(f"{k} {v}건" for k, v in type_distribution.items())
        fact_lines.append(f"유형 분포: {dist_str}")

    user_prompt = (
        "실제 데이터:\n" + "\n".join(fact_lines) +
        "\n\n위 데이터만 사용해 자연스러운 한국어 문장 1~2개를 작성하라."
    )

    try:
        resp = await chat(
            messages=[
                {"role": "user", "content": f"[System]\n{_SUMMARY_SYSTEM_PROMPT}\n\n[User]\n{user_prompt}"},
            ],
            model=settings.llm_default_model,
            temperature=0.9,
            max_tokens=150,
            use_cache=False,
        )
        text = resp.text.strip()
        return text or _build_summary_fallback(topic, total, keywords, sort_label)
    except Exception as e:
        logger.warning("검색 결과 요약 생성 실패, 폴백 사용: %s", e)
        return _build_summary_fallback(topic, total, keywords, sort_label)


async def _search_chroma_local(
    query: str,
    size: int,
    pub_year_start: int | None = None,
    scope: str | None = None,
) -> list[PaperSearchItem]:
    try:
        service = get_chroma_search_service()
        return await service.search(
            query=query,
            n_results=size,
            pub_year_start=pub_year_start,
            scope=scope,
        )
    except Exception:
        return []


async def search_papers_service(
    request: SearchPapersRequest,
    db: AsyncSession | None = None,
) -> SearchPapersResponse:
    items: list[PaperSearchItem] = []

    chroma_items = await _search_chroma_local(query=request.query, size=request.size)
    items.extend(chroma_items)

    if db is not None:
        try:
            items = await enrich_items_with_credibility(items, db)
        except Exception:
            pass

    items = _apply_scoring(items)
    items = _sort_items(items)
    items = _apply_sort_order(items, request.sort_order)

    return SearchPapersResponse(
        search_id="search_combined_001",
        items=items[: request.size],
    )


async def execute_search(
    search_params: dict,
    db: AsyncSession | None = None,
    *,
    filter_paper_type: str | None = None,
    filter_year: int | None = None,
    filter_kci: bool | None = None,
    filter_sci: bool | None = None,
    sort_order: str = "relevance",
    topic: str | None = None,
) -> dict:
    """SearchParams 기반 실행 검색 — 슬롯 대화 완료 후 호출"""
    keywords = search_params.get("keywords") or []
    scope = search_params.get("scope", "ANY")
    pub_year_start = search_params.get("pub_year_start")
    research_purpose = search_params.get("research_purpose", "")
    size = 20

    base_query = " ".join(keywords)
    # C-2: LLM 쿼리 확장
    expanded_query = await expand_query_for_embedding(base_query)

    items: list[PaperSearchItem] = []

    chroma_items = await _search_chroma_local(
        query=expanded_query,
        size=size,
        pub_year_start=pub_year_start,
        scope=scope,
    )
    items.extend(chroma_items)

    if db is not None:
        try:
            items = await enrich_items_with_credibility(items, db)
        except Exception:
            pass

    items = _apply_scoring(items, research_purpose=research_purpose)
    items = _sort_items(items)

    # DB에서 degree 조회 → paper_type 세분화
    db_extra: dict = {}
    if db is not None:
        try:
            db_extra = await get_paper_cards_batch(db, [i.paper_id for i in items])
        except Exception:
            pass

    def _resolve_type(item: PaperSearchItem) -> str | None:
        degree = (db_extra.get(item.paper_id) or {}).get("degree")
        return paper_type_label(resolve_paper_type(item.db_code, degree))

    if filter_paper_type and filter_paper_type != "전체":
        items = [i for i in items if _resolve_type(i) == filter_paper_type]

    if filter_year is not None:
        items = [i for i in items if i.year == filter_year]

    if filter_kci is not None:
        items = [i for i in items if i.credibility.kci_registered is not None and i.credibility.kci_registered == filter_kci]

    if filter_sci is not None:
        items = [i for i in items if i.credibility.sci_indexed is not None and i.credibility.sci_indexed == filter_sci]

    items = _apply_sort_order(items, sort_order)

    papers = []
    for item in items[:size]:
        papers.append({
            "paper_id":       item.paper_id,
            "title":          item.title,
            "authors":        [a.name for a in item.authors],
            "pub_year":       item.year,
            "journal":        item.journal_name,
            "paper_type":     _resolve_type(item),
            "abstract":       item.abstract,
            "keywords":       item.keywords,
            "doi":            item.doi,
            "scope_badge":    _resolve_scope_badge(item.credibility),
            "citation_count": item.credibility.citation_count,
            "relevance_score": item.score,
            "trust_badge":    item.credibility.badge if item.credibility.badge != "unknown" else None,
            "keyword_map_data": None,
        })

    type_distribution = dict(Counter(p["paper_type"] or "기타" for p in papers))
    summary_message = await generate_result_summary(
        topic=topic or "",
        total=len(papers),
        keywords=keywords,
        type_distribution=type_distribution,
        sort_order=sort_order,
    )

    return {
        "papers": papers,
        "total": len(papers),
        "type_distribution": type_distribution,
        "summary_message": summary_message,
        "search_id": str(uuid.uuid4()),
    }
