"""Neo4j + Postgres Paper 노드/행 paper_type 분류.

분류 규칙:
  JAKO* → journal
  JAFO (DBCode) → journal
  DIKO* (degree=국내박사) → doctoral_thesis
  DIKO* (degree=국내석사) → master_thesis

※ excluded_non_stem 라벨은 apply_non_stem_filter.py에서 별도 처리.

사용법:
  python scripts/classify_paper_types.py
  python scripts/classify_paper_types.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
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
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from neo4j import GraphDatabase
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.settings import settings


def _run(session, query: str, **params) -> list:
    return list(session.run(query, **params))


def _classify_neo4j(driver, *, dry_run: bool) -> dict[str, int]:
    with driver.session() as session:
        current = _run(session, """
            MATCH (p:Paper)
            RETURN
                CASE
                    WHEN p.cn STARTS WITH 'JAKO' THEN 'JAKO'
                    WHEN p.cn STARTS WITH 'DIKO' THEN 'DIKO'
                    WHEN p.db_code = 'JAFO'       THEN 'JAFO'
                    ELSE 'OTHER'
                END AS grp,
                count(p) AS cnt
            ORDER BY cnt DESC
        """)
        print("=== 현재 Paper 분포 ===")
        for r in current:
            print(f"  {r['grp']}: {r['cnt']}개")

        if dry_run:
            print("\n[dry-run] Neo4j 쓰기 생략")
            return {}

        r1 = _run(session, """
            MATCH (p:Paper) WHERE p.cn STARTS WITH 'JAKO'
            SET p.paper_type = 'journal'
            RETURN count(p) AS cnt
        """)
        r2 = _run(session, """
            MATCH (p:Paper) WHERE p.db_code = 'JAFO'
            SET p.paper_type = 'journal'
            RETURN count(p) AS cnt
        """)
        r3 = _run(session, """
            MATCH (p:Paper) WHERE p.cn STARTS WITH 'DIKO' AND p.degree = '국내박사'
            SET p.paper_type = 'doctoral_thesis'
            RETURN count(p) AS cnt
        """)
        r4 = _run(session, """
            MATCH (p:Paper) WHERE p.cn STARTS WITH 'DIKO' AND p.degree = '국내석사'
            SET p.paper_type = 'master_thesis'
            RETURN count(p) AS cnt
        """)
        counts = {
            "JAKO→journal": r1[0]["cnt"],
            "JAFO→journal": r2[0]["cnt"],
            "DIKO→doctoral_thesis": r3[0]["cnt"],
            "DIKO→master_thesis": r4[0]["cnt"],
        }
        return counts


async def _classify_postgres(*, dry_run: bool) -> dict[str, int]:
    async with AsyncSessionLocal() as session:
        if dry_run:
            r = await session.execute(text("""
                SELECT db_code, paper_type, count(*) FROM papers
                GROUP BY db_code, paper_type ORDER BY db_code, paper_type
            """))
            print("\n[dry-run] Postgres 현재 상태:")
            for row in r.fetchall():
                print(f"  {row[0]:5s} {str(row[1] or 'NULL'):22s} {row[2]}건")
            return {}

        r1 = await session.execute(text("""
            UPDATE papers SET paper_type = 'journal', updated_at = now()
            WHERE db_code IN ('JAKO', 'JAFO')
              AND (paper_type IS NULL OR paper_type NOT IN ('journal', 'excluded_non_stem'))
            RETURNING id
        """))
        jako_jafo = len(r1.fetchall())

        r2 = await session.execute(text("""
            UPDATE papers SET paper_type = 'doctoral_thesis', updated_at = now()
            WHERE db_code = 'DIKO' AND degree = '국내박사'
              AND (paper_type IS NULL OR paper_type = 'thesis_excluded')
            RETURNING id
        """))
        doctoral = len(r2.fetchall())

        r3 = await session.execute(text("""
            UPDATE papers SET paper_type = 'master_thesis', updated_at = now()
            WHERE db_code = 'DIKO' AND degree = '국내석사'
              AND (paper_type IS NULL OR paper_type = 'thesis_excluded')
            RETURNING id
        """))
        master = len(r3.fetchall())

        await session.commit()
        return {
            "JAKO/JAFO→journal": jako_jafo,
            "DIKO→doctoral_thesis": doctoral,
            "DIKO→master_thesis": master,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Neo4j + Postgres paper_type 분류")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        print("─── Neo4j ───")
        neo4j_counts = _classify_neo4j(driver, dry_run=args.dry_run)
        if neo4j_counts:
            for k, v in neo4j_counts.items():
                print(f"  {k}: {v}개")

        print("\n─── Postgres ───")
        pg_counts = asyncio.run(_classify_postgres(dry_run=args.dry_run))
        if pg_counts:
            for k, v in pg_counts.items():
                print(f"  {k}: {v}건")

        if not args.dry_run:
            print("\n[완료] Neo4j + Postgres 양쪽 동기화됨")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
