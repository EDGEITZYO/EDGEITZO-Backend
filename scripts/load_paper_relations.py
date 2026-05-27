"""paper_details.json → paper_similar / paper_references DB 적재.

in_service 매칭 우선순위:
  1. cited_cn / similar_cn → papers.id 직접 매칭
  2. doi → papers.doi 매칭 (URL 형태 정규화 후 IN 쿼리)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))

import asyncpg

from app.core.settings import settings

DETAILS_PATH = _PROJECT_ROOT / "data" / "parsed" / "paper_details.json"

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
)


def _normalize_doi(doi: str) -> str:
    for prefix in _DOI_PREFIXES:
        if doi.lower().startswith(prefix.lower()):
            return doi[len(prefix):]
    return doi


def _doi_variants(bare: str) -> list[str]:
    return [bare] + [p + bare for p in _DOI_PREFIXES]


async def _build_doi_index(conn: asyncpg.Connection) -> dict[str, str]:
    """papers.doi → papers.id 역인덱스 (bare DOI 기준)."""
    rows = await conn.fetch("SELECT id, doi FROM papers WHERE doi IS NOT NULL")
    index: dict[str, str] = {}
    for r in rows:
        bare = _normalize_doi(r["doi"])
        index[bare] = r["id"]
    return index


async def _build_cn_set(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT id FROM papers")
    return {r["id"] for r in rows}


async def main() -> None:
    if not DETAILS_PATH.exists():
        print(f"[오류] {DETAILS_PATH} 없음. recollect_papers_detail.py 먼저 실행하세요.")
        sys.exit(1)

    data = json.loads(DETAILS_PATH.read_text(encoding="utf-8"))
    details: dict[str, dict] = data["details"]
    print(f"=== paper_relations 적재 시작: {len(details)}건 ===")

    db_url = settings.database_url.replace("postgresql+asyncpg", "postgresql")
    conn = await asyncpg.connect(db_url)

    try:
        print("  DB 인덱스 로딩...")
        cn_set = await _build_cn_set(conn)
        doi_index = await _build_doi_index(conn)
        print(f"  papers: {len(cn_set)}건 CN, {len(doi_index)}건 DOI 인덱스")

        # 기존 데이터 truncate
        await conn.execute("TRUNCATE TABLE paper_similar, paper_references RESTART IDENTITY")
        print("  기존 데이터 삭제 완료")

        similar_rows: list[dict] = []
        ref_rows: list[dict] = []

        for source_cn, detail in details.items():
            if source_cn not in cn_set:
                continue

            for s in detail.get("similar", []):
                scn = s.get("similar_cn")
                internal_id: str | None = None
                if scn and scn in cn_set:
                    internal_id = scn
                similar_rows.append({
                    "source_cn": source_cn,
                    "similar_cn": scn,
                    "title": s.get("title"),
                    "author": s.get("author"),
                    "pubyear": s.get("pubyear"),
                    "issn": s.get("issn"),
                    "material_type": s.get("material_type"),
                    "internal_paper_id": internal_id,
                })

            for r in detail.get("references", []):
                ccn = r.get("cited_cn")
                rdoi = r.get("doi")
                internal_id = None
                # 1) CN 매칭
                if ccn and ccn in cn_set:
                    internal_id = ccn
                # 2) DOI 매칭 (CN 미매칭 시)
                elif rdoi:
                    bare = _normalize_doi(rdoi)
                    internal_id = doi_index.get(bare)
                ref_rows.append({
                    "source_cn": source_cn,
                    "cited_cn": ccn,
                    "title": r.get("title"),
                    "author": r.get("author"),
                    "journal": r.get("journal"),
                    "doi": rdoi,
                    "pubyear": r.get("pubyear"),
                    "issn": r.get("issn"),
                    "material_type": r.get("material_type"),
                    "internal_paper_id": internal_id,
                })

        # Bulk insert
        print(f"  paper_similar 적재: {len(similar_rows)}건...")
        if similar_rows:
            await conn.executemany(
                """
                INSERT INTO paper_similar
                  (source_cn, similar_cn, title, author, pubyear, issn,
                   material_type, internal_paper_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                [
                    (
                        r["source_cn"], r["similar_cn"], r["title"], r["author"],
                        r["pubyear"], r["issn"], r["material_type"], r["internal_paper_id"],
                    )
                    for r in similar_rows
                ],
            )

        print(f"  paper_references 적재: {len(ref_rows)}건...")
        if ref_rows:
            await conn.executemany(
                """
                INSERT INTO paper_references
                  (source_cn, cited_cn, title, author, journal, doi, pubyear,
                   issn, material_type, internal_paper_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                [
                    (
                        r["source_cn"], r["cited_cn"], r["title"], r["author"],
                        r["journal"], r["doi"], r["pubyear"], r["issn"],
                        r["material_type"], r["internal_paper_id"],
                    )
                    for r in ref_rows
                ],
            )

        # 통계
        sim_total = await conn.fetchval("SELECT COUNT(*) FROM paper_similar")
        sim_matched = await conn.fetchval(
            "SELECT COUNT(*) FROM paper_similar WHERE internal_paper_id IS NOT NULL"
        )
        ref_total = await conn.fetchval("SELECT COUNT(*) FROM paper_references")
        ref_matched = await conn.fetchval(
            "SELECT COUNT(*) FROM paper_references WHERE internal_paper_id IS NOT NULL"
        )
        src_with_refs = await conn.fetchval(
            "SELECT COUNT(DISTINCT source_cn) FROM paper_references"
        )
        src_with_sim = await conn.fetchval(
            "SELECT COUNT(DISTINCT source_cn) FROM paper_similar"
        )

        print(f"\n=== 적재 완료 ===")
        print(f"paper_similar   : {sim_total}건 (in_service 매칭 {sim_matched}건)")
        print(f"paper_references: {ref_total}건 (in_service 매칭 {ref_matched}건)")
        print(f"참고문헌 보유 논문: {src_with_refs}건 / {len(details)}건")
        print(f"유사문헌 보유 논문: {src_with_sim}건 / {len(details)}건")

    finally:
        await conn.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
