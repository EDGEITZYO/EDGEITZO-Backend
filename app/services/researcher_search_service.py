from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.schemas.researcher import (
    RecentResearcherSearchItem,
    RecentResearcherSearchResponse,
    ResearcherGraphEdge,
    ResearcherGraphNode,
    ResearcherGraphResponse,
    ResearcherSearchItem,
    ResearcherSearchResponse,
    ResearcherSearchType,
)

_REDIS_DB = 7
_RECENT_KEY = "researcher_searches:{user_id}"
_RECENT_LIMIT = 6
_RECENT_TTL_SECONDS = 86400 * 7


_NAME_COUNT_SQL = text(
    """
    SELECT count(1)
    FROM researchers r
    WHERE lower(replace(coalesce(r.author_name_kor, ''), ' ', '')) LIKE :norm_pattern
       OR lower(replace(coalesce(r.author_name_eng, ''), ' ', '')) LIKE :norm_pattern
    """
)


_NAME_SEARCH_SQL = text(
    """
    SELECT
        r.researcher_id,
        r.source,
        r.scienceon_cn,
        r.author_name_kor,
        r.author_name_eng,
        r.institution_current,
        r.institution_dept,
        r.keywords,
        coalesce(r.total_papers, r.corpus_paper_count, 0) AS total_papers,
        r.total_citations,
        r.citation_source,
        coalesce(r.corpus_paper_count, 0) AS corpus_paper_count,
        r.first_pubyear,
        r.last_pubyear,
        CASE
            WHEN lower(replace(coalesce(r.author_name_kor, ''), ' ', '')) = :norm_query THEN 3
            WHEN lower(replace(coalesce(r.author_name_eng, ''), ' ', '')) = :norm_query THEN 3
            WHEN lower(replace(coalesce(r.author_name_kor, ''), ' ', '')) LIKE :norm_prefix THEN 2
            WHEN lower(replace(coalesce(r.author_name_eng, ''), ' ', '')) LIKE :norm_prefix THEN 2
            ELSE 1
        END AS name_score,
        count(1) OVER() AS total_count
    FROM researchers r
    WHERE lower(replace(coalesce(r.author_name_kor, ''), ' ', '')) LIKE :norm_pattern
       OR lower(replace(coalesce(r.author_name_eng, ''), ' ', '')) LIKE :norm_pattern
    ORDER BY
        name_score DESC,
        coalesce(r.total_papers, r.corpus_paper_count, 0) DESC,
        r.total_citations DESC NULLS LAST,
        r.author_name_kor ASC NULLS LAST,
        r.author_name_eng ASC NULLS LAST
    LIMIT :limit OFFSET :offset
    """
)


_FIELD_SEARCH_SQL = text(
    """
    WITH internal_matches AS (
        SELECT
            rp.researcher_id,
            count(DISTINCT p.id) AS field_paper_count,
            array_remove(
                array_agg(DISTINCT kw.value)
                FILTER (WHERE lower(coalesce(kw.value, '')) LIKE :pattern),
                NULL
            ) AS matched_keywords
        FROM researcher_papers rp
        JOIN papers p ON p.id = rp.paper_id
        LEFT JOIN LATERAL unnest(
            coalesce(p.keywords_ko, ARRAY[]::varchar[])
            || coalesce(p.keywords_en, ARRAY[]::varchar[])
        ) AS kw(value) ON true
        WHERE lower(coalesce(p.title, '')) LIKE :pattern
           OR lower(coalesce(p.title_en, '')) LIKE :pattern
           OR lower(coalesce(p.abstract, '')) LIKE :pattern
           OR lower(coalesce(kw.value, '')) LIKE :pattern
        GROUP BY rp.researcher_id
    ),
    external_matches AS (
        SELECT
            x.researcher_id,
            count(DISTINCT x.id) AS field_paper_count,
            array_remove(
                array_agg(DISTINCT kw.value)
                FILTER (WHERE lower(coalesce(kw.value, '')) LIKE :pattern),
                NULL
            ) AS matched_keywords
        FROM researcher_external_papers x
        LEFT JOIN LATERAL unnest(
            coalesce(x.keywords, ARRAY[]::varchar[])
            || coalesce(x.categories, ARRAY[]::varchar[])
        ) AS kw(value) ON true
        WHERE lower(coalesce(x.title, '')) LIKE :pattern
           OR lower(coalesce(x.journal, '')) LIKE :pattern
           OR lower(coalesce(kw.value, '')) LIKE :pattern
        GROUP BY x.researcher_id
    )
    SELECT
        r.researcher_id,
        r.source,
        r.scienceon_cn,
        r.author_name_kor,
        r.author_name_eng,
        r.institution_current,
        r.institution_dept,
        r.keywords,
        coalesce(r.total_papers, r.corpus_paper_count, 0) AS total_papers,
        r.total_citations,
        r.citation_source,
        coalesce(r.corpus_paper_count, 0) AS corpus_paper_count,
        r.first_pubyear,
        r.last_pubyear,
        coalesce(im.field_paper_count, 0) + coalesce(em.field_paper_count, 0) AS field_paper_count,
        coalesce(rk.keyword_match_count, 0) AS keyword_match_count,
        rk.matched_keywords AS researcher_matched_keywords,
        im.matched_keywords AS internal_matched_keywords,
        em.matched_keywords AS external_matched_keywords,
        (
            coalesce(rk.keyword_match_count, 0) * 10
            + coalesce(im.field_paper_count, 0)
            + coalesce(em.field_paper_count, 0)
            + least(coalesce(r.total_citations, 0), 1000) / 1000.0
        ) AS relevance_score,
        count(1) OVER() AS total_count
    FROM researchers r
    LEFT JOIN internal_matches im ON im.researcher_id = r.researcher_id
    LEFT JOIN external_matches em ON em.researcher_id = r.researcher_id
    LEFT JOIN LATERAL (
        SELECT
            count(DISTINCT kw.value) AS keyword_match_count,
            array_remove(array_agg(DISTINCT kw.value), NULL) AS matched_keywords
        FROM unnest(coalesce(r.keywords, ARRAY[]::varchar[])) AS kw(value)
        WHERE lower(coalesce(kw.value, '')) LIKE :pattern
    ) rk ON true
    WHERE coalesce(rk.keyword_match_count, 0) > 0
       OR coalesce(im.field_paper_count, 0) > 0
       OR coalesce(em.field_paper_count, 0) > 0
    ORDER BY
        relevance_score DESC,
        field_paper_count DESC,
        r.total_citations DESC NULLS LAST,
        coalesce(r.total_papers, r.corpus_paper_count, 0) DESC,
        r.author_name_kor ASC NULLS LAST
    LIMIT :limit OFFSET :offset
    """
)


def _clean_query(query: str) -> str:
    return " ".join(query.strip().split())


def _name_params(query: str) -> dict[str, str]:
    norm_query = query.replace(" ", "").lower()
    return {
        "norm_query": norm_query,
        "norm_pattern": f"%{norm_query}%",
        "norm_prefix": f"{norm_query}%",
    }


def _field_params(query: str) -> dict[str, str]:
    return {"pattern": f"%{query.lower()}%"}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, tuple):
        return [str(item) for item in value if item]
    return [str(value)]


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _item_from_row(row: Any, *, field: bool) -> ResearcherSearchItem:
    matched_keywords = []
    if field:
        matched_keywords = _unique(
            [
                *_as_list(row.get("researcher_matched_keywords")),
                *_as_list(row.get("internal_matched_keywords")),
                *_as_list(row.get("external_matched_keywords")),
            ]
        )

    return ResearcherSearchItem(
        researcher_id=row["researcher_id"],
        source=row["source"],
        scienceon_cn=row.get("scienceon_cn"),
        author_name_kor=row.get("author_name_kor"),
        author_name_eng=row.get("author_name_eng"),
        institution_current=row.get("institution_current"),
        institution_dept=row.get("institution_dept"),
        keywords=_as_list(row.get("keywords")),
        total_papers=int(row.get("total_papers") or 0),
        total_citations=row.get("total_citations"),
        citation_source=row.get("citation_source"),
        corpus_paper_count=int(row.get("corpus_paper_count") or 0),
        first_pubyear=row.get("first_pubyear"),
        last_pubyear=row.get("last_pubyear"),
        field_paper_count=int(row.get("field_paper_count") or 0) if field else None,
        matched_keywords=matched_keywords,
        relevance_score=round(float(row.get("relevance_score") or 0), 4) if field else None,
    )


async def _has_name_matches(db: AsyncSession, query: str) -> bool:
    count = await db.scalar(_NAME_COUNT_SQL, _name_params(query))
    return bool(count)


async def search_researchers(
    db: AsyncSession,
    query: str,
    *,
    page: int = 1,
    size: int = 20,
) -> ResearcherSearchResponse:
    cleaned = _clean_query(query)
    search_type: ResearcherSearchType = "name" if await _has_name_matches(db, cleaned) else "field"
    limit = size
    offset = (page - 1) * size

    if search_type == "name":
        params = _name_params(cleaned) | {"limit": limit, "offset": offset}
        rows = (await db.execute(_NAME_SEARCH_SQL, params)).mappings().all()
        items = [_item_from_row(row, field=False) for row in rows]
    else:
        params = _field_params(cleaned) | {"limit": limit, "offset": offset}
        rows = (await db.execute(_FIELD_SEARCH_SQL, params)).mappings().all()
        items = [_item_from_row(row, field=True) for row in rows]

    total = int(rows[0]["total_count"]) if rows else 0
    return ResearcherSearchResponse(
        query=cleaned,
        search_type=search_type,
        total=total,
        page=page,
        size=size,
        items=items,
    )


def _display_name(item: ResearcherSearchItem) -> str:
    return item.author_name_kor or item.author_name_eng or item.researcher_id


def build_researcher_graph(query: str, items: list[ResearcherSearchItem]) -> ResearcherGraphResponse:
    center_key = f"field:{query}"
    nodes: list[ResearcherGraphNode] = [
        ResearcherGraphNode(
            key=center_key,
            node_type="field",
            label=query,
        )
    ]
    edges: list[ResearcherGraphEdge] = []

    max_score = max((item.relevance_score or 0 for item in items), default=0) or 1
    for item in items:
        node_key = f"researcher:{item.researcher_id}"
        nodes.append(
            ResearcherGraphNode(
                key=node_key,
                node_type="researcher",
                label=_display_name(item),
                researcher_id=item.researcher_id,
                author_name_kor=item.author_name_kor,
                author_name_eng=item.author_name_eng,
                institution_current=item.institution_current,
                institution_dept=item.institution_dept,
                keywords=item.keywords,
                total_citations=item.total_citations,
                citation_source=item.citation_source,
                field_paper_count=item.field_paper_count,
                relevance_score=item.relevance_score,
            )
        )
        edges.append(
            ResearcherGraphEdge(
                source=center_key,
                target=node_key,
                edge_type="field_relevance",
                weight=round(float((item.relevance_score or 0) / max_score), 4),
                shared_keywords=item.matched_keywords,
            )
        )

    return ResearcherGraphResponse(
        query=query,
        total=len(items),
        nodes=nodes,
        edges=edges,
    )


def save_recent_researcher_search(user_id: str, query: str, search_type: ResearcherSearchType) -> None:
    cleaned = _clean_query(query)
    if not cleaned:
        return

    try:
        redis = get_redis(_REDIS_DB)
        key = _RECENT_KEY.format(user_id=user_id)
        existing = json.loads(redis.get(key) or "[]")
        existing = [item for item in existing if item.get("query") != cleaned]
        existing.insert(
            0,
            {
                "query": cleaned,
                "search_type": search_type,
                "searched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        redis.set(key, json.dumps(existing[:_RECENT_LIMIT], ensure_ascii=False), ex=_RECENT_TTL_SECONDS)
    except Exception:
        return


def get_recent_researcher_searches(user_id: str) -> RecentResearcherSearchResponse:
    try:
        redis = get_redis(_REDIS_DB)
        raw = redis.get(_RECENT_KEY.format(user_id=user_id))
        items = json.loads(raw or "[]")
    except Exception:
        items = []

    return RecentResearcherSearchResponse(
        items=[RecentResearcherSearchItem(**item) for item in items[:_RECENT_LIMIT]]
    )
