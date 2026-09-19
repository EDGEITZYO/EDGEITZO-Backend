"""국내(KCI) 논문을 필요한 순간에 서비스 논문으로 적재한다.

in_service의 정의:
  "papers 테이블에 미리 적재돼 있는가"가 아니라 **KCI 논문 ID(ART…)가 있는가**로 판정한다.
  미리 적재한 논문만 in_service로 치면, 적재할 때마다 그 논문들의 참고문헌이 새로 "코퍼스 밖"이
  되어 적재 범위가 끝없이 늘어난다. KCI ID가 있으면 articleDetail 한 번(실측 0.1~0.3초)으로
  서지정보·초록·참고문헌을 모두 받을 수 있으므로, 상세페이지나 그래프 확장이 요청되는 순간
  받아서 적재한다. 결과적으로 사용자가 실제로 본 논문만 적재된다.

  KCI ID가 없는 참고문헌(REF…, OpenAlex W…)은 해외 논문으로, 서지정보만 보여준다.

적재 범위 (materialize_domestic_paper 한 번에):
  1. Postgres papers 행 (없으면 KCI에서 받아 INSERT, source='kci_citation')
  2. Neo4j (:Paper) 노드
  3. 그 논문의 참고문헌 — 이미 papers에 있는 논문이면 CITES 엣지, 아니면
     paper_citation_external_refs 행(ART…면 다음에 누를 때 같은 경로로 적재됨)
  4. 다른 논문의 external_refs에 이 논문을 가리키던 행이 있으면 CITES 엣지로 옮기고 행은 지운다
     — "external_refs에는 그래프 노드가 아닌 대상만 있다"는 불변식을 지킨다.
     그래야 같은 논문이 CITES 노드와 external 노드로 두 번 나오지 않는다.

  검색 코퍼스(JSON + ChromaDB 임베딩)에는 넣지 않는다. 검색 결과가 사용자의 클릭 이력에 따라
  달라지면 안 되기 때문이다.

  모든 쓰기는 멱등(ON CONFLICT DO NOTHING / MERGE)이라 같은 논문을 동시에 요청해도 안전하다.

주의 — Neo4j Aura는 로컬·운영이 같은 인스턴스다. 노드·CITES는 모든 환경에 한 번에 반영되지만
papers 행·참고문헌 행은 환경별 Postgres에만 들어간다. 그래서 "참고문헌을 받았는가"는 Postgres
(papers.kci_refs_loaded_at)로 판단한다. Neo4j의 refs_loaded_at은 참고용 기록일 뿐 판단에 쓰지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from neo4j.exceptions import ConstraintError, TransientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.neo4j_client import get_neo4j_driver
from app.core.redis import get_redis
from app.core.settings import settings
from app.integrations.kci.researcher_client import KCIArticle, parse_article_detail

logger = logging.getLogger(__name__)

_REDIS_DB = 7
_ART_ID_RE = re.compile(r"^ART\d+$")
_KOREAN_RE = re.compile(r"[가-힣]")

SOURCE_LABEL = "kci_citation"


def is_domestic_key(key: Optional[str]) -> bool:
    """KCI 논문 ID(ART…)면 국내 논문 — 상세페이지를 만들 수 있으므로 in_service다."""
    return bool(key) and bool(_ART_ID_RE.match(key))


# ---------------------------------------------------------------------------
# KCI articleDetail
# ---------------------------------------------------------------------------

@dataclass
class KciReference:
    external_id: str  # arti-id가 있으면 ART…, 없으면 refebibl-id(REF…)
    arti_id: Optional[str]
    title: Optional[str]
    authors: Optional[list[str]]
    journal: Optional[str]
    doi: Optional[str]
    pubyear: Optional[int]


@dataclass
class KciPaper:
    article: KCIArticle
    publisher: Optional[str] = None
    references: list[KciReference] = field(default_factory=list)


_YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")


def _year_or_none(raw: Optional[str]) -> Optional[int]:
    """참고문헌 연도는 입력 오류가 섞여 온다(실측: "197519751984"). 앞에서 처음 나오는 연도만 쓴다."""
    match = _YEAR_RE.search(raw or "")
    return int(match.group(1)) if match else None


def parse_kci_paper(xml_text: str) -> Optional[KciPaper]:
    article = parse_article_detail(xml_text)
    if article is None or not article.art_id:
        return None
    root = ET.fromstring(xml_text)
    refs: list[KciReference] = []
    for ref in root.iter("reference"):
        arti_id = (ref.get("arti-id") or "").strip() or None
        external_id = arti_id or (ref.get("refebibl-id") or "").strip() or None
        if not external_id:
            continue
        authors: list[str] = []
        for node in ref.findall("author"):
            authors.extend(n.strip() for n in (node.text or "").split(";") if n.strip())
        refs.append(
            KciReference(
                external_id=external_id,
                arti_id=arti_id,
                title=((ref.findtext("title") or "").strip() or None),
                authors=[a[:300] for a in authors] or None,
                journal=((ref.findtext("journal-name") or "").strip()[:500] or None),
                doi=((ref.findtext("doi") or "").strip()[:200] or None),
                pubyear=_year_or_none(ref.findtext("pubi-year")),
            )
        )
    publisher = (root.findtext(".//journalInfo/publisher-name") or "").strip() or None
    return KciPaper(article=article, publisher=publisher, references=refs)


# 실패한 ID는 잠시 다시 부르지 않는다 — KCI 장애 때 그래프 요청마다 타임아웃(8초)을 기다리지 않도록
_FAILED_TTL_SECONDS = 300


def _failed_key(art_id: str) -> str:
    return f"kci_article:failed:{art_id}"


def _recently_failed(art_id: str) -> bool:
    try:
        return bool(get_redis(_REDIS_DB).exists(_failed_key(art_id)))
    except Exception:
        return False


def _mark_failed(art_id: str) -> None:
    try:
        get_redis(_REDIS_DB).set(_failed_key(art_id), "1", ex=_FAILED_TTL_SECONDS)
    except Exception:
        pass


async def fetch_kci_paper(
    art_id: str, client: Optional[httpx.AsyncClient] = None, *, retry_failed: bool = False
) -> Optional[KciPaper]:
    if not settings.kci_api_key or (not retry_failed and _recently_failed(art_id)):
        return None
    params = {"apiCode": "articleDetail", "key": settings.kci_api_key, "id": art_id}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=settings.paper_citation_external_fetch_timeout_seconds) as own:
                response = await own.get(settings.kci_base_url, params=params)
        else:
            response = await client.get(settings.kci_base_url, params=params)
        response.raise_for_status()
        fetched = parse_kci_paper(response.text)
    except Exception:
        logger.warning("KCI articleDetail 조회 실패: %s", art_id, exc_info=True)
        fetched = None
    if fetched is None:
        _mark_failed(art_id)
    return fetched


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

_PAPER_COLUMNS = "id, kci_art_id, db_code, title, title_en, pubyear, doi, journal_name, citation_count, kci_refs_loaded_at"

_JOURNAL_BY_ISSN_SQL = "SELECT id FROM journals WHERE issn && :forms ORDER BY sci_indexed DESC LIMIT 1"
_JOURNAL_BY_NAME_SQL = (
    "SELECT id FROM journals WHERE lower(btrim(title)) = lower(btrim(:journal)) ORDER BY sci_indexed DESC LIMIT 1"
)

_INSERT_SQL = """
INSERT INTO papers (id, source_type, kci_art_id, issn, title, title_en, abstract, abstract_en,
                    authors, keywords_ko, keywords_en, pubyear, pubdate, paper_type,
                    citation_count, journal_id, journal_name, publisher, db_code, source, doi,
                    created_at, updated_at)
VALUES (:id, 'kci', :id, :issn, :title, :title_en, :abstract, :abstract_en,
        :authors, :keywords_ko, :keywords_en, :pubyear, :pubdate, '학술 저널',
        :citation_count, :journal_id, :journal_name, :publisher, 'JAKO', :source, :doi,
        now(), now())
ON CONFLICT DO NOTHING
"""


async def resolve_papers(db: AsyncSession, keys: list[str]) -> dict[str, dict[str, Any]]:
    """key(papers.id 또는 KCI art_id) → papers 행. 코퍼스 논문은 id가 ScienceON CN이고
    kci_art_id에 ART…를 따로 들고 있어 두 컬럼을 모두 본다."""
    if not keys:
        return {}
    rows = (
        await db.execute(
            text(f"SELECT {_PAPER_COLUMNS} FROM papers WHERE id = ANY(:keys) OR kci_art_id = ANY(:keys)"),
            {"keys": keys},
        )
    ).mappings().all()
    wanted = set(keys)
    resolved: dict[str, dict[str, Any]] = {}
    for row in rows:
        row = dict(row)
        for k in (row["id"], row["kci_art_id"]):
            if k in wanted:
                # id 일치가 kci_art_id 일치보다 우선
                if k not in resolved or resolved[k]["id"] != k:
                    resolved[k] = row
    return resolved


def _issn_forms(issn: Optional[str]) -> Optional[list[str]]:
    if not issn:
        return None
    raw = issn.strip()
    forms = {raw, raw.replace("-", "")}
    if "-" not in raw and len(raw) == 8:
        forms.add(f"{raw[:4]}-{raw[4:]}")
    return sorted(forms)


_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")


def _bare_doi(doi: Optional[str]) -> Optional[str]:
    if not doi or not doi.strip():
        return None
    doi = doi.strip()
    lowered = doi.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            return doi[len(prefix):]
    return doi


def _normalize_doi(doi: Optional[str]) -> Optional[str]:
    """코퍼스 규약(https://doi.org/…)으로 저장한다."""
    bare = _bare_doi(doi)
    return f"https://doi.org/{bare}" if bare else None


def _doi_forms(doi: Optional[str]) -> list[str]:
    """papers.doi에는 bare(10.…)와 URL 형태가 섞여 있다(연구자 논문 편입분은 KCI 원문 그대로)."""
    bare = _bare_doi(doi)
    return [bare, *(f"{prefix}{bare}" for prefix in _DOI_PREFIXES)] if bare else []


def _norm_title(title: Optional[str]) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", title or "").casefold())


def _same_title(row: Any, article: KCIArticle) -> bool:
    ours = {_norm_title(row.title), _norm_title(row.title_en)} - {""}
    theirs = {_norm_title(article.title), _norm_title(article.title_eng)} - {""}
    return bool(ours & theirs)


async def _insert_paper(db: AsyncSession, fetched: KciPaper, *, source: str, keep_doi: bool = True) -> None:
    a = fetched.article
    journal_id = None
    forms = _issn_forms(a.issn)
    if forms:
        journal_id = (await db.execute(text(_JOURNAL_BY_ISSN_SQL), {"forms": forms})).scalar()
    if journal_id is None and a.journal:
        journal_id = (await db.execute(text(_JOURNAL_BY_NAME_SQL), {"journal": a.journal})).scalar()

    keywords_ko = [k[:200] for k in a.keywords if _KOREAN_RE.search(k)]
    keywords_en = [k[:200] for k in a.keywords if not _KOREAN_RE.search(k)]
    month = (a.pubmonth or "").zfill(2) if a.pubmonth else None
    pubdate = None
    if a.pubyear:
        pubdate = f"{a.pubyear}-{month}-01" if month and month != "00" else f"{a.pubyear}-01-01"

    await db.execute(
        text(_INSERT_SQL),
        {
            "id": a.art_id,
            "issn": (a.issn or "").replace("-", "")[:20] or None,
            "title": (a.title or a.title_eng or a.art_id)[:1000],
            "title_en": (a.title_eng or None) and a.title_eng[:1000],
            "abstract": a.abstract,
            "abstract_en": a.abstract_eng,
            "authors": [x.name[:300] for x in a.authors] or None,
            "keywords_ko": keywords_ko or None,
            "keywords_en": keywords_en or None,
            "pubyear": a.pubyear,
            "pubdate": pubdate,
            "citation_count": a.citation_count or 0,
            "journal_id": journal_id,
            "journal_name": (a.journal or None) and a.journal[:500],
            "publisher": (fetched.publisher or None) and fetched.publisher[:500],
            "source": source,
            "doi": _normalize_doi(a.doi) if keep_doi else None,
        },
    )
    # 연구자 이력에 같은 논문이 있으면 상세 이동이 되도록 연결
    await db.execute(
        text(
            "UPDATE researcher_external_papers SET internal_paper_id = :pid "
            "WHERE external_id = :pid AND internal_paper_id IS NULL"
        ),
        {"pid": a.art_id},
    )


async def _insert_external_refs(db: AsyncSession, source_cn: str, refs: list[KciReference]) -> None:
    if not refs:
        return
    await db.execute(
        text(
            """
            INSERT INTO paper_citation_external_refs
                (source_cn, direction, external_source, external_id, title, authors, journal, doi, pubyear, created_at)
            SELECT :source_cn, 'reference', 'kci', r.external_id, r.title, r.authors, r.journal, r.doi, r.pubyear, now()
            FROM jsonb_to_recordset(CAST(:rows AS jsonb))
                 AS r(external_id text, title text, authors varchar[], journal text, doi text, pubyear int)
            ON CONFLICT (source_cn, direction, external_id) DO NOTHING
            """
        ),
        {
            "source_cn": source_cn,
            "rows": _json_rows(
                [
                    {
                        "external_id": r.external_id,
                        "title": (r.title or None) and r.title[:1000],
                        "authors": r.authors,
                        "journal": r.journal,
                        "doi": r.doi,
                        "pubyear": r.pubyear,
                    }
                    for r in refs
                ]
            ),
        },
    )


def _json_rows(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Neo4j (sync 드라이버 — asyncio.to_thread로 호출)
# ---------------------------------------------------------------------------

def _neo4j_node_exists(cn: str) -> bool:
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            return session.run("MATCH (p:Paper {cn: $cn}) RETURN 1 AS x", cn=cn).single() is not None
    finally:
        driver.close()


def _neo4j_write(
    nodes: list[dict[str, Any]],
    cites: list[tuple[str, str]],
    loaded_cn: Optional[str],
) -> set[tuple[str, str]]:
    """노드 MERGE → CITES MERGE → 참고문헌 적재 완료 표시. 실제로 생긴(양 끝 노드가 존재한)
    엣지 목록을 돌려준다 — external_refs 행은 엣지가 확인된 것만 지운다."""
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            if nodes:
                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (p:Paper {cn: row.cn})
                    ON CREATE SET p.db_code = row.db_code, p.title = row.title, p.title_en = row.title_en,
                                  p.pubyear = row.pubyear, p.doi = row.doi, p.journal_name = row.journal_name,
                                  p.citation_count = row.citation_count
                    """,
                    rows=nodes,
                ).consume()
            created: set[tuple[str, str]] = set()
            if cites:
                records = session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (a:Paper {cn: row.a})
                    MATCH (b:Paper {cn: row.b})
                    MERGE (a)-[:CITES]->(b)
                    RETURN row.a AS a, row.b AS b
                    """,
                    rows=[{"a": a, "b": b} for a, b in cites],
                )
                created = {(r["a"], r["b"]) for r in records}
            if loaded_cn:
                session.run(
                    "MATCH (p:Paper {cn: $cn}) SET p.refs_loaded_at = datetime()", cn=loaded_cn
                ).consume()
            return created
    finally:
        driver.close()


def _node_row(paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "cn": paper["id"],
        "db_code": paper.get("db_code"),
        "title": paper.get("title"),
        "title_en": paper.get("title_en"),
        "pubyear": paper.get("pubyear"),
        "doi": paper.get("doi"),
        "journal_name": paper.get("journal_name"),
        "citation_count": paper.get("citation_count"),
    }


def _invalidate_graph_cache(cns: set[str]) -> None:
    if not cns:
        return
    try:
        r = get_redis(_REDIS_DB)
        keys = [f"paper_citation:subgraph:in_service:{d}:{cn}" for cn in cns for d in ("reference", "citing")]
        r.delete(*keys)
    except Exception:
        logger.warning("인용관계 그래프 캐시 무효화 실패", exc_info=True)


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

async def materialize_domestic_paper(
    db: AsyncSession,
    key: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    source: str = SOURCE_LABEL,
    retry_failed: bool = False,
) -> Optional[str]:
    """key(papers.id 또는 ART…)에 해당하는 논문을 상세페이지·인용관계 그래프에 쓸 수 있게 만든다.

    반환: 서비스 논문 ID(papers.id). 논문이 없고 KCI에서도 못 받으면 None.
    이미 적재·연결이 끝난 논문이면 DB 조회 한 번과 Neo4j 조회 한 번으로 끝난다.
    """
    paper = (await resolve_papers(db, [key])).get(key)
    fetched: Optional[KciPaper] = None

    if paper is None:
        if not is_domestic_key(key):
            return None
        fetched = await fetch_kci_paper(key, client, retry_failed=retry_failed)
        if fetched is None or not (fetched.article.title or fetched.article.title_eng):
            return None
        # 같은 DOI의 논문이 다른 ID로 이미 있으면 그 논문이다(papers.doi 유니크)
        # 같은 DOI의 논문이 다른 ID로 이미 있으면 같은 논문일 수 있다(papers.doi 유니크).
        # 단 KCI는 학술지 호(issue) 단위 DOI를 그 호의 논문마다 똑같이 붙여 오는 경우가 있어
        # (실측: 서로 다른 3편이 10.12925/jkocs.2013.30.3.371) 제목까지 같아야 같은 논문으로 본다.
        keep_doi = True
        forms = _doi_forms(fetched.article.doi)
        if forms:
            same = (
                await db.execute(text("SELECT id, title, title_en FROM papers WHERE doi = ANY(:forms) LIMIT 1"), {"forms": forms})
            ).first()
            if same is not None:
                if _same_title(same, fetched.article):
                    paper = (await resolve_papers(db, [same.id])).get(same.id)
                else:
                    keep_doi = False
        if paper is None:
            await _insert_paper(db, fetched, source=source, keep_doi=keep_doi)
            await db.commit()
            paper = (await resolve_papers(db, [key])).get(key)
            if paper is None:
                return None

    paper_id = paper["id"]
    node_exists = await asyncio.to_thread(_neo4j_node_exists, paper_id)
    # 참고문헌을 KCI에서 받아 와야 하는 건 KCI ID로 식별되는 논문뿐이다. ScienceON CN으로
    # 들어온 코퍼스 논문은 참고문헌이 이미 적재돼 있다(scripts/load_paper_citation_external_refs_kci.py).
    # 받았는지는 이 환경 Postgres(papers.kci_refs_loaded_at)로 본다 — 참고문헌 행이 환경별 Postgres에
    # 들어가므로, 여러 환경이 같이 쓰는 Neo4j에 표시하면 다른 환경이 받은 것으로 착각한다.
    needs_refs = is_domestic_key(paper_id) and paper.get("kci_refs_loaded_at") is None
    # 요청 key가 이 논문의 id/kci_art_id와 다르면(같은 논문이 다른 ID로 있던 경우) 그 key를
    # 가리키던 참고문헌 행도 이 노드로 옮긴다
    aliases = {key} - {paper_id, paper.get("kci_art_id")}
    if node_exists and not needs_refs and not aliases:
        return paper_id

    refs: list[KciReference] = []
    if needs_refs:
        if fetched is None:
            fetched = await fetch_kci_paper(paper["kci_art_id"] or paper_id, client, retry_failed=retry_failed)
        if fetched is None:
            # 참고문헌은 못 받았어도 노드는 만든다(상세페이지·피인용 그래프는 동작). 다음 요청 때 재시도.
            needs_refs = False
        else:
            refs = fetched.references

    await _link(db, paper, refs, mark_loaded=needs_refs, aliases=aliases)
    return paper_id


async def _link(
    db: AsyncSession,
    paper: dict[str, Any],
    refs: list[KciReference],
    *,
    mark_loaded: bool,
    aliases: set[str] = frozenset(),
) -> None:
    paper_id = paper["id"]
    resolved = await resolve_papers(db, [r.arti_id for r in refs if r.arti_id])
    in_graph_refs = [r for r in refs if r.arti_id and r.arti_id in resolved and resolved[r.arti_id]["id"] != paper_id]
    outside_refs = [r for r in refs if not (r.arti_id and r.arti_id in resolved)]

    ref_papers = {resolved[r.arti_id]["id"]: resolved[r.arti_id] for r in in_graph_refs}
    node_papers = {paper_id: paper, **ref_papers}

    # 이 논문들을 가리키던 다른 논문의 external_refs 행 → CITES로 옮길 대상
    target_ids: dict[str, str] = {}
    for p in node_papers.values():
        target_ids[p["id"]] = p["id"]
        if p.get("kci_art_id"):
            target_ids[p["kci_art_id"]] = p["id"]
    for alias in aliases:
        target_ids[alias] = paper_id
    incoming = (
        await db.execute(
            text(
                "SELECT id, source_cn, direction, external_id FROM paper_citation_external_refs "
                "WHERE external_id = ANY(:ids)"
            ),
            {"ids": list(target_ids)},
        )
    ).mappings().all()

    cites: set[tuple[str, str]] = {(paper_id, pid) for pid in ref_papers}
    row_edges: dict[int, tuple[str, str]] = {}
    for row in incoming:
        node = target_ids[row["external_id"]]
        if row["source_cn"] == node:
            continue
        edge = (row["source_cn"], node) if row["direction"] == "reference" else (node, row["source_cn"])
        cites.add(edge)
        row_edges[row["id"]] = edge

    write_args = ([_node_row(p) for p in node_papers.values()], sorted(cites), paper_id if mark_loaded else None)
    try:
        created = await asyncio.to_thread(_neo4j_write, *write_args)
    except (ConstraintError, TransientError):
        # 같은 논문을 동시에 적재하면 MERGE가 유일 제약(paper_cn_unique)에서 부딪힌다. 다시 하면 MATCH로 끝난다.
        created = await asyncio.to_thread(_neo4j_write, *write_args)

    moved = [rid for rid, edge in row_edges.items() if edge in created]
    if moved:
        await db.execute(text("DELETE FROM paper_citation_external_refs WHERE id = ANY(:ids)"), {"ids": moved})
    await _insert_external_refs(db, paper_id, outside_refs)
    if mark_loaded:
        await db.execute(text("UPDATE papers SET kci_refs_loaded_at = now() WHERE id = :id"), {"id": paper_id})
    await db.commit()

    touched = {paper_id, *ref_papers, *(row["source_cn"] for row in incoming)}
    await asyncio.to_thread(_invalidate_graph_cache, touched)
