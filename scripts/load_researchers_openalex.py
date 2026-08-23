"""해외 학술지(JAFO) 저자 적재 — OpenAlex.

KCI는 국내 등재지만 다룬다. 코퍼스 저자 2,855명 중 876명은 해외지에만 실려 KCI에 아예 없다.
OpenAlex는 저자를 이미 식별해 두었으므로(author id) 이름 클러스터링이 필요 없고,
JAFO 논문 150편이 DOI를 100% 보유해 접합률이 높다.

  works/{doi}   authorships[].author.id / institutions / author_position
  authors/{id}  works_count / cited_by_count / topics / last_known_institutions

사용법:
  python scripts/load_researchers_openalex.py --dry-run
  python scripts/load_researchers_openalex.py --limit 10
  python scripts/load_researchers_openalex.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

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
        if _k.strip():
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import httpx
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.models.researcher import Researcher, ResearcherExternalPaper, ResearcherPaper

CHECKPOINT_PATH = PROJECT_ROOT / "data" / "checkpoints" / "researcher_openalex.json"
OPENALEX_BASE = "https://api.openalex.org"
OPENALEX_MAILTO = "yuri12120771@gmail.com"  # polite pool
RATE_LIMIT_DELAY = 0.15
MAX_RETRIES = 4
# 저자 1명의 전체 저작을 다 받으면 수천 건이 되는 경우가 있다 — 프로필 표시에 필요한 만큼만.
MAX_WORKS_PER_AUTHOR = 100

# 역할 표기는 KCI(제1/교신/참여/단독)에 맞춘다 — 화면에서 두 소스가 같은 라벨을 쓰게.
_POSITION_TO_ROLE = {"first": "제1", "last": "교신", "middle": "참여"}

_ROLE_KEY = "role"


def _short_id(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1] if url else None


def _load_ckpt() -> dict:
    if CHECKPOINT_PATH.exists():
        try:
            return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def _save_ckpt(data: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict | None:
    merged = {"mailto": OPENALEX_MAILTO, **(params or {})}
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url, params=merged, timeout=25.0)
            if resp.status_code == 429:
                await asyncio.sleep(2 ** attempt * 2)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:  # noqa: BLE001 — 네트워크 계열 전반
            if attempt == MAX_RETRIES - 1:
                return None
            await asyncio.sleep(2 ** attempt)
    return None


async def _load_target_papers(limit: int | None) -> list[tuple[str, str]]:
    """OpenAlex로 저자를 채울 논문 = DOI가 있고 KCI 쪽에서 못 다룬 것(해외지)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, doi FROM papers "
                    "WHERE doi IS NOT NULL AND kci_art_id IS NULL ORDER BY id"
                )
            )
        ).all()
    pairs = [(r.id, r.doi) for r in rows]
    return pairs[:limit] if limit else pairs


def _normalize_doi(doi: str) -> str:
    return doi.replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()


async def run(*, dry_run: bool, limit: int | None) -> None:
    papers = await _load_target_papers(limit)
    print(f"[준비] 대상 논문 {len(papers):,}편 (DOI 보유 · KCI 미커버)")
    if dry_run:
        print("[dry-run] 호출 없이 종료")
        return

    ckpt = _load_ckpt()
    stats = Counter()
    # author_id -> 누적
    people: dict[str, dict] = {}
    links: dict[tuple[str, str], dict] = {}

    async with httpx.AsyncClient() as client:
        for idx, (paper_id, doi) in enumerate(papers, start=1):
            cached = ckpt.get(paper_id)
            if cached is None:
                await asyncio.sleep(RATE_LIMIT_DELAY)
                work = await _get(client, f"{OPENALEX_BASE}/works/https://doi.org/{_normalize_doi(doi)}")
                if work is None:
                    stats["work_miss"] += 1
                    ckpt[paper_id] = {"authorships": []}
                    continue
                cached = {
                    "authorships": [
                        {
                            "id": _short_id(a.get("author", {}).get("id")),
                            "name": a.get("author", {}).get("display_name"),
                            "orcid": a.get("author", {}).get("orcid"),
                            "institutions": [i.get("display_name") for i in a.get("institutions", []) if i.get("display_name")],
                            "position": a.get("author_position"),
                        }
                        for a in work.get("authorships", [])
                        if a.get("author", {}).get("id")
                    ]
                }
                ckpt[paper_id] = cached
                if idx % 20 == 0:
                    _save_ckpt(ckpt)

            if not cached["authorships"]:
                stats["no_authors"] += 1
                continue
            stats["ok"] += 1
            for order, authorship in enumerate(cached["authorships"], start=1):
                aid = authorship["id"]
                person = people.setdefault(
                    aid,
                    {"name": authorship["name"], "institutions": [], "orcid": authorship.get("orcid"), "papers": set()},
                )
                for inst in authorship["institutions"]:
                    if inst not in person["institutions"]:
                        person["institutions"].append(inst)
                person["papers"].add(paper_id)
                links[(f"oa:{aid}", paper_id)] = {
                    "researcher_id": f"oa:{aid}",
                    "paper_id": paper_id,
                    "author_order": order,
                    _ROLE_KEY: _POSITION_TO_ROLE.get(authorship.get("position") or ""),
                    "institution_at_time": authorship["institutions"][0] if authorship["institutions"] else None,
                }
            if idx % 25 == 0:
                print(f"  논문 {idx}/{len(papers)} — 저자 {len(people):,}명 누적")

        _save_ckpt(ckpt)
        print(f"[논문] 처리 {stats['ok']:,} / 저자없음 {stats['no_authors']} / OpenAlex 미보유 {stats['work_miss']}")
        print(f"[논문] 고유 저자 {len(people):,}명, 링크 {len(links):,}건")

        # 저자 상세 — 총 논문/피인용/연구주제
        print("[저자] OpenAlex 저자 상세 수집")
        author_ckpt_key = "__authors__"
        author_cache = ckpt.setdefault(author_ckpt_key, {})
        for n, aid in enumerate(people, start=1):
            if aid in author_cache:
                continue
            await asyncio.sleep(RATE_LIMIT_DELAY)
            data = await _get(client, f"{OPENALEX_BASE}/authors/{aid}")
            if data is None:
                author_cache[aid] = {}
            else:
                insts = data.get("last_known_institutions") or []
                author_cache[aid] = {
                    "works_count": data.get("works_count"),
                    "cited_by_count": data.get("cited_by_count"),
                    "topics": [t.get("display_name") for t in (data.get("topics") or [])[:10] if t.get("display_name")],
                    "last_institution": insts[0].get("display_name") if insts else None,
                    "display_name": data.get("display_name"),
                }
            if n % 25 == 0:
                _save_ckpt(ckpt)
                print(f"  저자 {n}/{len(people)}")
        _save_ckpt(ckpt)

    # ── DB 반영 ──────────────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        for aid, person in people.items():
            meta = author_cache.get(aid) or {}
            current = meta.get("last_institution") or (person["institutions"][0] if person["institutions"] else None)
            values = {
                "researcher_id": f"oa:{aid}",
                "source": "openalex",
                "author_name_eng": meta.get("display_name") or person["name"],
                "author_name_kor": None,
                "institution_current": current,
                "institution_history": person["institutions"] or None,
                "keywords": meta.get("topics") or None,
                "total_papers": meta.get("works_count"),
                "total_citations": meta.get("cited_by_count"),
                "citation_source": "openalex",
                "corpus_paper_count": len(person["papers"]),
                "expanded_at": datetime.now(timezone.utc),
            }
            stmt = pg_insert(Researcher).values(**values).on_conflict_do_update(
                index_elements=["researcher_id"],
                set_={k: v for k, v in values.items() if k != "researcher_id"} | {"updated_at": sa.func.now()},
            )
            await session.execute(stmt)
        await session.commit()

        link_rows = list(links.values())
        for start in range(0, len(link_rows), 500):
            stmt = pg_insert(ResearcherPaper).values(link_rows[start : start + 500])
            stmt = stmt.on_conflict_do_update(
                index_elements=["researcher_id", "paper_id"],
                set_={
                    "author_order": stmt.excluded.author_order,
                    "role": stmt.excluded.role,
                    "institution_at_time": stmt.excluded.institution_at_time,
                },
            )
            await session.execute(stmt)
        await session.commit()

    with_cites = sum(1 for a in author_cache.values() if (a or {}).get("cited_by_count"))
    print(f"[완료] 연구자 {len(people):,}명 / 링크 {len(link_rows):,}건 / 피인용>0 {with_cites:,}명")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAlex 기반 해외지 저자 적재")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
