"""자체 950건 코퍼스 밖 국내(KCI) 참고문헌 후보를 실제 논문으로 신규 적재하고,
그로 인해 새로 채워지는 CITES 엣지까지 함께 반영한다.

배경:
  scripts/load_paper_citations_from_kci.py는 기존 논문들의 참고문헌 중 우리 코퍼스
  "밖"에 있는 것들은 전부 버렸다. 그중 다수는 실제로는 KCI에 정식 등재된 국내 논문이다.
  이 스크립트는 그 후보들 중 "여러 편에 동시에 인용되는" 순으로 상위 N건을 골라 실제
  논문으로 신규 적재하여, 인용관계 그래프의 국내 커버리지를 확장한다.

후보 선정 기준:
  data/checkpoints/kci_all_reference_artiids.json (각 기존 논문의 전체 참고문헌 arti-id 목록,
  코퍼스 매칭 여부 무관하게 보존된 원본)에서, 이미 코퍼스에 있는 art_id를 제외한 나머지를
  "몇 편의 기존 논문에게 동시에 인용됐는지" 기준 내림차순 정렬 후 상위 N건 채택.

적재 범위 (의도적 스코프 제한):
  - Postgres papers 테이블: 논문 상세페이지가 그대로 동작하도록 전체 서지정보 적재
  - Neo4j (:Paper) 노드: 인용관계 그래프가 이 논문을 찾을 수 있도록 cn(=art_id) 키로 생성
  - (:Paper)-[:CITES]->(:Paper) 엣지: 기존 논문 <-> 신규 논문, 신규 논문 <-> 신규 논문 양방향 모두 반영
  - ChromaDB 임베딩, Neo4j HAS_KEYWORD/AUTHORED/PUBLISHED_IN/RELATED_TO는 포함하지 않음
    (일반 검색/키워드맵에는 노출되지 않고, 인용관계 그래프 및 직접 URL 접근으로만 도달 가능 —
    의도적 결정. 필요해지면 별도로 논의)

체크포인트: data/checkpoints/kci_new_reference_papers.json (art_id 단위, 재실행 시 --limit을
늘리면 이미 적재된 건 건너뛰고 신규 후보만 추가 처리 — 점진적 확장 가능)

사용법:
  python scripts/add_kci_reference_papers.py --dry-run --limit 30
  python scripts/add_kci_reference_papers.py --limit 30
  python scripts/add_kci_reference_papers.py --limit 50   # 나중에 확장 — 이미 적재된 30건은 스킵
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import xmltodict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from neo4j import GraphDatabase
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.models.paper import Paper

KCI_CITATIONS_CHECKPOINT = PROJECT_ROOT / "data" / "checkpoints" / "kci_citations.json"
ALL_REFS_CHECKPOINT = PROJECT_ROOT / "data" / "checkpoints" / "kci_all_reference_artiids.json"
NEW_PAPERS_CHECKPOINT = PROJECT_ROOT / "data" / "checkpoints" / "kci_new_reference_papers.json"

RATE_LIMIT_DELAY = 0.35
MAX_RETRIES = 4
_KOREAN_RE = re.compile(r"[가-힣]")


# ---------------------------------------------------------------------------
# 후보 선정
# ---------------------------------------------------------------------------

def rank_candidates() -> list[str]:
    with open(KCI_CITATIONS_CHECKPOINT, encoding="utf-8") as f:
        kci = json.load(f)
    own_art_ids = {v["art_id"] for v in kci.values() if v.get("status") == "ok"}

    with open(ALL_REFS_CHECKPOINT, encoding="utf-8") as f:
        all_refs: dict[str, list[str]] = json.load(f)

    counter: Counter[str] = Counter()
    for arti_ids in all_refs.values():
        for a in arti_ids:
            if a not in own_art_ids:
                counter[a] += 1

    return [art_id for art_id, _ in counter.most_common()]


def existing_corpus_outgoing_refs() -> dict[str, list[str]]:
    """cn -> [참고문헌 arti-id, ...] (코퍼스 매칭 여부 무관, 전체)."""
    with open(ALL_REFS_CHECKPOINT, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# KCI API
# ---------------------------------------------------------------------------

def _kci_params(api_code: str, **kwargs) -> dict:
    return {"apiCode": api_code, "key": settings.kci_api_key, **kwargs}


async def _get_xml(client: httpx.AsyncClient, params: dict, *, retries: int = MAX_RETRIES) -> dict | None:
    for attempt in range(retries):
        try:
            r = await client.get(settings.kci_base_url, params=params, timeout=20.0)
            if r.status_code in (429, 503):
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            if r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return xmltodict.parse(r.text)
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [에러] {e}")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


def _as_list(node: Any) -> list:
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _text(node: Any) -> str | None:
    if node is None:
        return None
    if isinstance(node, dict):
        return node.get("#text")
    return str(node)


def _title_by_lang(title_group: dict, lang: str) -> str | None:
    titles = _as_list((title_group or {}).get("article-title"))
    for t in titles:
        if isinstance(t, dict) and t.get("@lang") == lang:
            return t.get("#text")
    return None


def _abstract_by_lang(abstract_group: dict, lang: str) -> str | None:
    abstracts = _as_list((abstract_group or {}).get("abstract"))
    for a in abstracts:
        if isinstance(a, dict) and a.get("@lang") == lang:
            return a.get("#text")
    return None


def _split_keywords(keyword_group: dict) -> tuple[list[str], list[str]]:
    keywords = _as_list((keyword_group or {}).get("keyword"))
    ko, en = [], []
    for k in keywords:
        if not k:
            continue
        (ko if _KOREAN_RE.search(k) else en).append(k)
    return ko, en


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if doi.lower().startswith(prefix):
            return f"https://doi.org/{doi[len(prefix):]}"
    return doi


async def fetch_article_detail(client: httpx.AsyncClient, art_id: str) -> dict | None:
    """articleDetail 전체 서지정보 + referenceInfo(arti-id 목록)를 파싱."""
    parsed = await _get_xml(client, _kci_params("articleDetail", id=art_id))
    if not parsed:
        return None

    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        article_info = record.get("articleInfo", {})
        journal_info = record.get("journalInfo", {})

        title_group = article_info.get("title-group", {})
        abstract_group = article_info.get("abstract-group", {})
        keyword_ko, keyword_en = _split_keywords(article_info.get("keyword-group", {}))

        authors = []
        for a in _as_list(article_info.get("author-group", {}).get("author")):
            if isinstance(a, dict) and a.get("name"):
                authors.append(a["name"])

        cit_node = article_info.get("citation-count", {})
        citation_count = 0
        if isinstance(cit_node, dict):
            try:
                citation_count = int(cit_node.get("@kci") or 0)
            except (TypeError, ValueError):
                citation_count = 0

        references = _as_list(record.get("referenceInfo", {}).get("reference")) if record.get("referenceInfo") else []
        reference_arti_ids = [r.get("@arti-id") for r in references if isinstance(r, dict) and r.get("@arti-id")]

        return {
            "art_id": art_id,
            "title": _title_by_lang(title_group, "original"),
            "title_en": _title_by_lang(title_group, "english"),
            "abstract": _abstract_by_lang(abstract_group, "original"),
            "abstract_en": _abstract_by_lang(abstract_group, "english"),
            "authors": authors,
            "keywords_ko": keyword_ko,
            "keywords_en": keyword_en,
            "doi": _normalize_doi(_text(article_info.get("doi"))),
            "pubyear": int(journal_info.get("pub-year")) if journal_info.get("pub-year") else None,
            "journal_name": journal_info.get("journal-name"),
            "citation_count": citation_count,
            "reference_arti_ids": reference_arti_ids,
        }
    except Exception as e:
        print(f"  [파싱 오류] {art_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# 체크포인트
# ---------------------------------------------------------------------------

def _load_checkpoint() -> dict[str, Any]:
    if NEW_PAPERS_CHECKPOINT.exists():
        return json.loads(NEW_PAPERS_CHECKPOINT.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict[str, Any]) -> None:
    NEW_PAPERS_CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    NEW_PAPERS_CHECKPOINT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Postgres / Neo4j 적재
# ---------------------------------------------------------------------------

async def _insert_paper(detail: dict) -> None:
    now = datetime.now(timezone.utc)
    record = {
        "id": detail["art_id"],
        "source_type": "kci",
        "scienceon_cn": None,
        "semantic_scholar_id": None,
        "doi": detail["doi"],
        "issn": None,
        "title": detail["title"] or detail["title_en"] or detail["art_id"],
        "title_en": detail["title_en"],
        "abstract": detail["abstract"],
        "abstract_en": detail["abstract_en"],
        "authors": detail["authors"] or None,
        "keywords_ko": detail["keywords_ko"] or None,
        "keywords_en": detail["keywords_en"] or None,
        "pubyear": detail["pubyear"],
        "pubdate": None,
        "kci_art_id": detail["art_id"],
        "paper_type": "학술 저널",
        "citation_count": detail["citation_count"],
        "journal_id": None,
        "journal_name": detail["journal_name"],
        "db_code": "JAKO",
        "source": "kci_reference_expansion",
        "created_at": now,
        "updated_at": now,
    }
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(Paper).values(**record).on_conflict_do_nothing()
        await session.execute(stmt)
        await session.commit()


def _neo4j_merge_paper(driver, detail: dict) -> None:
    query = """
    MERGE (p:Paper {cn: $cn})
    ON CREATE SET
        p.db_code = 'JAKO',
        p.title = $title,
        p.title_en = $title_en,
        p.abstract = $abstract,
        p.abstract_en = $abstract_en,
        p.doi = $doi,
        p.pubyear = $pubyear,
        p.journal_name = $journal_name,
        p.citation_count = $citation_count
    """
    with driver.session() as session:
        session.run(
            query,
            cn=detail["art_id"],
            title=detail["title"],
            title_en=detail["title_en"],
            abstract=detail["abstract"],
            abstract_en=detail["abstract_en"],
            doi=detail["doi"],
            pubyear=detail["pubyear"],
            journal_name=detail["journal_name"],
            citation_count=detail["citation_count"],
        )


def _neo4j_merge_citation(driver, citing_cn: str, cited_cn: str) -> None:
    query = """
    MATCH (a:Paper {cn: $citing_cn})
    MATCH (b:Paper {cn: $cited_cn})
    MERGE (a)-[:CITES]->(b)
    """
    with driver.session() as session:
        session.run(query, citing_cn=citing_cn, cited_cn=cited_cn)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(description="KCI 참고문헌 후보 신규 적재 + CITES 엣지 확장")
    parser.add_argument("--limit", type=int, default=30, help="누적 적재 목표 건수 (기본 30, 재실행 시 늘리면 증분만 처리)")
    parser.add_argument("--dry-run", action="store_true", help="DB/Neo4j 쓰기 생략, 후보 목록만 출력")
    args = parser.parse_args()

    if not settings.kci_api_key:
        print("[오류] KCI_API_KEY가 .env에 설정되지 않았습니다.")
        sys.exit(1)

    ranked = rank_candidates()
    checkpoint = _load_checkpoint()
    already_done = set(checkpoint.keys())

    target = ranked[: args.limit]
    todo = [a for a in target if a not in already_done]

    print(f"=== 누적 목표 {args.limit}건 | 이미 적재됨 {len([a for a in target if a in already_done])}건 | 신규 처리 {len(todo)}건 ===")

    if not todo:
        print("추가로 처리할 신규 후보가 없습니다 (--limit을 늘려서 재실행하세요).")
        return

    if args.dry_run:
        print("[dry-run] 아래 art_id들을 신규 적재 예정:")
        for a in todo:
            print(" ", a)
        return

    new_details: dict[str, dict] = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, art_id in enumerate(todo):
            await asyncio.sleep(RATE_LIMIT_DELAY)
            detail = await fetch_article_detail(client, art_id)
            if detail is None or not detail.get("title"):
                print(f"  [{i + 1}/{len(todo)}] {art_id} 조회 실패 — 스킵")
                continue
            new_details[art_id] = detail
            checkpoint[art_id] = {"title": detail["title"], "added_at": datetime.now(timezone.utc).isoformat()}
            print(f"  [{i + 1}/{len(todo)}] {art_id} {detail['title'][:40]}")

    print(f"\n[수집 완료] 신규 논문 메타데이터 {len(new_details)}건")

    print("[Postgres] 신규 논문 insert 중...")
    for detail in new_details.values():
        await _insert_paper(detail)
    print(f"[Postgres] {len(new_details)}건 insert 완료")

    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    try:
        print("[Neo4j] 신규 Paper 노드 MERGE 중...")
        for detail in new_details.values():
            _neo4j_merge_paper(driver, detail)

        all_new_art_ids = set(checkpoint.keys())  # 이번 실행 + 과거 누적 전부 포함 (재실행 확장 대비)
        own_art_ids_before = set()
        with open(KCI_CITATIONS_CHECKPOINT, encoding="utf-8") as f:
            kci = json.load(f)
        art_id_to_cn = {v["art_id"]: cn for cn, v in kci.items() if v.get("status") == "ok"}
        own_art_ids_before = set(art_id_to_cn.keys())

        edge_count = 0

        # (a) 기존 코퍼스 논문 -> 신규 논문
        existing_refs = existing_corpus_outgoing_refs()
        for cn, arti_ids in existing_refs.items():
            for a in arti_ids:
                if a in all_new_art_ids:
                    _neo4j_merge_citation(driver, cn, a)
                    edge_count += 1

        # (b), (c) 신규 논문 -> 기존 코퍼스 논문 / 신규 논문 -> 신규 논문
        for art_id, detail in new_details.items():
            for ref_arti_id in detail.get("reference_arti_ids", []):
                if ref_arti_id in own_art_ids_before:
                    cited_cn = art_id_to_cn[ref_arti_id]
                    _neo4j_merge_citation(driver, art_id, cited_cn)
                    edge_count += 1
                elif ref_arti_id in all_new_art_ids:
                    _neo4j_merge_citation(driver, art_id, ref_arti_id)
                    edge_count += 1

        print(f"[Neo4j] CITES 엣지 {edge_count}건 MERGE 완료 (기존과 중복되는 건 자동 스킵)")
    finally:
        driver.close()

    _save_checkpoint(checkpoint)
    print("[완료] 체크포인트 저장됨 — 나중에 --limit을 늘려 재실행하면 이어서 확장됩니다.")


if __name__ == "__main__":
    asyncio.run(main())
