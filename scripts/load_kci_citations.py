"""KCI Open API articleSearch + articleDetail → JAKO 인용수 + 결손 필드 보강.

흐름:
  JAKO(DBCode 기준) CN → DOI로 articleSearch → article-id(ART...) → articleDetail
  DOI 매칭 실패 시: 제목 완전일치(정규화)로 articleSearch 2차 시도
  2차도 실패하면 unmatched 기록

저장:
  - Neo4j Paper 노드: citation_count, doi, issn, journal_name 속성 업데이트
  - Postgres papers 테이블: citation_count, doi 업데이트 (scienceon_cn 매칭)

체크포인트: data/checkpoints/kci_citations.json
미매칭:     data/unmatched/kci_citations.json

사용법:
  python scripts/load_kci_citations.py
  python scripts/load_kci_citations.py --dry-run
  python scripts/load_kci_citations.py --limit 10
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
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

from sqlalchemy import select, text
from neo4j import GraphDatabase

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.models.paper import Paper

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "kci_citations.json"
UNMATCHED_PATH  = PROJECT_ROOT / "data" / "unmatched"  / "kci_citations.json"
PARSED_PATH     = PROJECT_ROOT / "data" / "parsed"     / "scienceon_enriched.json"

RATE_LIMIT_DELAY = 0.12   # 초당 ~8건
MAX_RETRIES      = 3


# ---------------------------------------------------------------------------
# KCI API
# ---------------------------------------------------------------------------

def _kci_params(api_code: str, **kwargs) -> dict:
    return {"apiCode": api_code, "key": settings.kci_api_key, **kwargs}


async def _get_xml(client: httpx.AsyncClient, params: dict, *, retries: int = MAX_RETRIES) -> dict | None:
    for attempt in range(retries):
        try:
            r = await client.get(settings.kci_base_url, params=params, timeout=20.0)
            if r.status_code == 429:
                wait = 2 ** attempt
                print(f"  [429] {wait}초 대기")
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            parsed = xmltodict.parse(r.text, force_list=("author", "keyword"))
            # KCI 에러 메시지 감지
            err = _extract_error(parsed)
            if err:
                print(f"  [KCI 오류] {err}")
                if "등록되지 않은 key" in err or "사용기간" in err:
                    sys.exit(f"[치명] KCI API 키 오류 — .env KCI_API_KEY 확인")
                return None
            return parsed
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [에러] {e}")
                return None
            await asyncio.sleep(2 ** attempt)
    return None


def _extract_error(parsed: dict) -> str | None:
    try:
        return parsed.get("error", {}).get("message") or parsed.get("result", {}).get("message")
    except Exception:
        return None


def _text_val(node: Any) -> str | None:
    if node is None:
        return None
    if isinstance(node, dict):
        return node.get("#text") or node.get("@kci") or None
    return str(node).strip() or None


def _normalize_doi(doi: str | None) -> str | None:
    """https://doi.org/10.xxx → 10.xxx 형태로 정규화."""
    if not doi:
        return None
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi or None


def _normalize_title(title: str | None) -> str:
    """제목 정규화: HTML 언이스케이프 → 태그 제거 → 공백 정규화 → 소문자화."""
    if not title:
        return ""
    t = html.unescape(title)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.lower()


async def search_article_id(
    client: httpx.AsyncClient,
    *,
    doi: str | None = None,
    title: str | None = None,
    author: str | None = None,
) -> str | None:
    """DOI 또는 제목+저자로 KCI article-id(ART...) 조회."""
    if doi:
        doi = _normalize_doi(doi)
        params = _kci_params("articleSearch", doi=doi)
    elif title:
        params = _kci_params("articleSearch", title=title[:100])
        if author:
            params["author"] = author.split(";")[0].strip()[:50]
    else:
        return None

    parsed = await _get_xml(client, params)
    if not parsed:
        return None

    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        articles = record.get("articleInfo")
        if isinstance(articles, list):
            articles = articles[0]
        art_id = (articles or {}).get("@article-id") or (articles or {}).get("article-id")
        return str(art_id).strip() if art_id else None
    except Exception:
        return None


async def _search_by_title_exact(client: httpx.AsyncClient, title_raw: str) -> str | None:
    """제목 완전일치(정규화)로 KCI article-id 조회.

    완전일치 1건만 채택. 0건 또는 복수 완전일치는 None(unmatched 유지).
    KCI 응답의 @lang=original 제목만 비교 대상으로 삼는다.
    """
    our_norm = _normalize_title(title_raw)
    if not our_norm:
        return None

    parsed = await _get_xml(client, _kci_params("articleSearch", title=our_norm[:100]))
    if not parsed:
        return None

    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        articles = record.get("articleInfo")
        if not articles:
            return None
        if isinstance(articles, dict):
            articles = [articles]

        def _original_title(article: dict) -> str:
            titles = article.get("title-group", {}).get("article-title", [])
            if isinstance(titles, dict):
                titles = [titles]
            for t in titles:
                if isinstance(t, dict) and t.get("@lang") == "original":
                    return t.get("#text", "")
            return ""

        exact = [
            a.get("@article-id")
            for a in articles
            if _normalize_title(_original_title(a)) == our_norm
        ]
        # 완전일치가 정확히 1건일 때만 채택
        return str(exact[0]).strip() if len(exact) == 1 else None
    except Exception:
        return None


async def fetch_article_detail(client: httpx.AsyncClient, art_id: str) -> dict | None:
    """articleDetail로 인용수 + 저널 정보 조회."""
    params = _kci_params("articleDetail", id=art_id)
    parsed = await _get_xml(client, params)
    if not parsed:
        return None

    try:
        record = parsed.get("MetaData", {}).get("outputData", {}).get("record", {})
        article_info = record.get("articleInfo", {})
        journal_info = record.get("journalInfo", {})

        # citation-count: <citation-count kci="4" wos="0">4</citation-count>
        cit_node = article_info.get("citation-count", {})
        kci_cit = None
        wos_cit = None
        if isinstance(cit_node, dict):
            kci_cit = _safe_int(cit_node.get("@kci"))
            wos_cit = _safe_int(cit_node.get("@wos"))
        elif cit_node:
            kci_cit = _safe_int(str(cit_node))

        fwci_node = article_info.get("fwci")
        fwci = None
        if isinstance(fwci_node, dict):
            fwci = _safe_float(fwci_node.get("#text"))
        elif fwci_node:
            fwci = _safe_float(str(fwci_node))

        doi_raw = _text_val(article_info.get("doi"))
        doi = doi_raw.strip() if doi_raw else None

        issn_raw = _text_val(journal_info.get("issn"))
        issn = issn_raw.strip() if issn_raw else None

        journal_name = _text_val(journal_info.get("journal-name"))
        kci_reg = _text_val(journal_info.get("kci-registration"))

        return {
            "kci_citation": kci_cit,
            "wos_citation": wos_cit,
            "citation_count": kci_cit,  # KCI 인용수 우선
            "fwci": fwci,
            "doi": doi,
            "issn": issn,
            "journal_name": journal_name,
            "kci_registration": kci_reg,
        }
    except Exception as e:
        print(f"  [파싱 오류] articleDetail: {e}")
        return None


def _safe_int(val: Any) -> int | None:
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _safe_float(val: Any) -> float | None:
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 체크포인트
# ---------------------------------------------------------------------------

def _load_checkpoint() -> dict[str, Any]:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(data: dict[str, Any]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Neo4j 업데이트
# ---------------------------------------------------------------------------

def _neo4j_update(driver, cn: str, detail: dict) -> None:
    sets = []
    params: dict[str, Any] = {"cn": cn}

    if detail.get("citation_count") is not None:
        sets.append("p.citation_count = $citation_count")
        params["citation_count"] = detail["citation_count"]
    if detail.get("doi"):
        sets.append("p.doi = $doi")
        params["doi"] = detail["doi"]
    if detail.get("issn"):
        sets.append("p.issn_kci = $issn")
        params["issn"] = detail["issn"]
    if detail.get("journal_name"):
        sets.append("p.journal_name_kci = $journal_name")
        params["journal_name"] = detail["journal_name"]
    if detail.get("kci_registration"):
        sets.append("p.kci_registration = $kci_registration")
        params["kci_registration"] = detail["kci_registration"]

    if not sets:
        return

    query = f"MATCH (p:Paper {{cn: $cn}}) SET {', '.join(sets)}"
    with driver.session() as session:
        session.run(query, **params)


# ---------------------------------------------------------------------------
# Postgres 업데이트
# ---------------------------------------------------------------------------

async def _postgres_update(cn: str, detail: dict) -> None:
    updates: dict[str, Any] = {}
    if detail.get("citation_count") is not None:
        updates["citation_count"] = detail["citation_count"]
    if detail.get("doi"):
        updates["doi"] = detail["doi"]
    if not updates:
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Paper).where(Paper.scienceon_cn == cn).limit(1)
        )
        paper = result.scalar_one_or_none()
        if paper:
            for k, v in updates.items():
                setattr(paper, k, v)
            paper.updated_at = datetime.now(timezone.utc)
            await session.commit()


# ---------------------------------------------------------------------------
# 메인 처리
# ---------------------------------------------------------------------------

async def process_papers(
    papers: list[dict],
    *,
    dry_run: bool,
    driver,
) -> None:
    checkpoint = _load_checkpoint()
    unmatched: list[dict] = []

    success = 0
    failed  = 0
    skipped = 0

    async with httpx.AsyncClient() as client:
        for i, paper in enumerate(papers):
            cn    = paper["CN"]
            doi   = paper.get("DOI")
            title = paper.get("Title") or paper.get("Title2", "")
            author = paper.get("Author", "")
            if isinstance(author, list):
                author = ";".join(author)

            # 체크포인트: 이미 처리한 CN 스킵
            if cn in checkpoint:
                skipped += 1
                continue

            await asyncio.sleep(RATE_LIMIT_DELAY)

            # 1차: DOI → articleSearch (DOI 없으면 title+author)
            art_id = await search_article_id(client, doi=doi, title=title if not doi else None, author=author if not doi else None)

            # 2차: DOI 실패 시 제목 완전일치 fallback
            if not art_id and title:
                await asyncio.sleep(RATE_LIMIT_DELAY)
                art_id = await _search_by_title_exact(client, title)
                if art_id:
                    print(f"    [title fallback] {cn} → {art_id}")

            if not art_id:
                print(f"  [미매칭] {cn} — article-id 없음")
                unmatched.append({"cn": cn, "doi": doi, "title": title, "reason": "article-id not found"})
                checkpoint[cn] = {"status": "unmatched"}
                failed += 1
                _save_checkpoint(checkpoint)
                continue

            detail = await fetch_article_detail(client, art_id)
            if not detail:
                print(f"  [실패] {cn} art_id={art_id} — detail 없음")
                unmatched.append({"cn": cn, "art_id": art_id, "reason": "articleDetail failed"})
                checkpoint[cn] = {"status": "detail_failed", "art_id": art_id}
                failed += 1
                _save_checkpoint(checkpoint)
                continue

            cit = detail.get("citation_count")
            print(f"  [{i+1}/{len(papers)}] {cn} art_id={art_id} citation={cit}")

            if not dry_run:
                _neo4j_update(driver, cn, detail)
                await _postgres_update(cn, detail)

            checkpoint[cn] = {
                "status": "ok",
                "art_id": art_id,
                "citation_count": cit,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_checkpoint(checkpoint)
            success += 1

    print(f"\n[완료] 성공={success} 미매칭={failed} 스킵(기존)={skipped}")
    print(f"  체크포인트: {CHECKPOINT_PATH}")

    if unmatched:
        UNMATCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNMATCHED_PATH.write_text(json.dumps(unmatched, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  미매칭 목록: {UNMATCHED_PATH} ({len(unmatched)}건)")


def _load_jako_papers(limit: int | None) -> list[dict]:
    data = json.loads(PARSED_PATH.read_text(encoding="utf-8-sig"))
    papers = [p for p in data.get("papers", []) if p.get("DBCode") == "JAKO"]
    if limit:
        papers = papers[:limit]
    return papers


def main() -> None:
    parser = argparse.ArgumentParser(description="KCI 인용수 + 결손 보강")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="처리할 논문 수 제한 (테스트용)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="체크포인트 초기화 후 전체 재처리")
    args = parser.parse_args()

    if not settings.kci_api_key:
        print("[오류] KCI_API_KEY가 .env에 설정되지 않았습니다.")
        sys.exit(1)

    if args.reset_checkpoint and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[초기화] 체크포인트 삭제")

    papers = _load_jako_papers(args.limit)
    print(f"=== KCI 인용수 보강 시작: JAKO {len(papers)}개 ===")
    if args.dry_run:
        print("[dry-run] Neo4j/Postgres 쓰기 생략\n")

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        asyncio.run(process_papers(papers, dry_run=args.dry_run, driver=driver))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
