"""KCI ID가 없는 코퍼스 논문의 참고문헌을 ScienceON에서 한 번 받아 paper_references에 저장한다.

ScienceON 토큰은 몇 시간마다 재발급해야 해서 서비스 실행 중에 부르면 만료 때마다 참고문헌 목록이
502가 된다. KCI ID가 있는 논문은 KCI(고정 키)로 받고, KCI ID가 없는 논문(코퍼스 JAKO 중 69편)은
이 스크립트로 미리 저장해 두고 GET /papers/{id}/references가 DB만 읽는다. 서비스 실행 중
ScienceON 호출은 없다.

코퍼스에 KCI ID 없는 논문을 새로 넣으면 이 스크립트를 다시 돌린다(대상 논문의 행만 교체).

사용법:
  python scripts/load_scienceon_references.py --dry-run
  python scripts/load_scienceon_references.py                      # 받아서 이 환경 DB에 저장 (유효 토큰 필요)
  python scripts/load_scienceon_references.py --export refs.json   # 받아서 파일로만 (유효 토큰 필요)
  python scripts/load_scienceon_references.py --import refs.json   # 파일을 이 환경 DB에 저장 (토큰 불필요)
"""
from __future__ import annotations

import argparse
import asyncio
import json
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

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.integrations.scienceon.client import ScienceOnClient
from app.integrations.scienceon.parser import parse_cited_references
from app.services.chroma_search_service import _PAPERS_PATH

CALL_PACING_SECONDS = 0.5

# KCI 경로(app/api/v1/paper.py get_paper_references)로 못 받는 논문 = id도 kci_art_id도 KCI ID가 아닌 국내 학술지
_TARGETS_SQL = """
SELECT id FROM papers
WHERE id = ANY(:corpus) AND db_code = 'JAKO'
  AND id NOT LIKE 'ART%' AND (kci_art_id IS NULL OR kci_art_id NOT LIKE 'ART%')
ORDER BY id
"""


async def _targets() -> list[str]:
    corpus = [p["CN"] for p in json.loads(_PAPERS_PATH.read_text(encoding="utf-8"))["papers"] if p.get("CN")]
    async with AsyncSessionLocal() as session:
        return list((await session.execute(text(_TARGETS_SQL), {"corpus": corpus})).scalars())


async def _fetch(targets: list[str]) -> dict[str, list[dict]]:
    client = ScienceOnClient()
    result: dict[str, list[dict]] = {}
    for cn in targets:
        await asyncio.sleep(CALL_PACING_SECONDS)
        xml = await client.browse_article(cn)  # 토큰 오류면 예외 — 일부만 저장되지 않게 전체 중단
        result[cn] = [
            {"title": r.title, "authors": r.authors, "year": r.year, "journal": r.journal, "doi": r.doi}
            for r in parse_cited_references(xml)
        ]
    return result


async def _store(refs_by_cn: dict[str, list[dict]]) -> None:
    rows = [
        {
            "source_cn": cn,
            "title": (r.get("title") or None) and r["title"][:1000],
            "author": "; ".join(r.get("authors") or []) or None,
            "journal": r.get("journal"),
            "doi": (r.get("doi") or None) and r["doi"][:200],
            "pubyear": r.get("year"),
        }
        for cn, refs in refs_by_cn.items()
        for r in refs
    ]
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM paper_references WHERE source_cn = ANY(:ids)"), {"ids": list(refs_by_cn)})
        if rows:
            await session.execute(
                text(
                    """
                    INSERT INTO paper_references (source_cn, title, author, journal, doi, pubyear)
                    SELECT r.source_cn, r.title, r.author, r.journal, r.doi, r.pubyear
                    FROM jsonb_to_recordset(CAST(:rows AS jsonb))
                         AS r(source_cn text, title text, author text, journal text, doi text, pubyear int)
                    """
                ),
                {"rows": json.dumps(rows, ensure_ascii=False)},
            )
        await session.commit()
    print(f"[저장] 논문 {len(refs_by_cn)}편 / 참고문헌 {len(rows)}행 (참고문헌 0건 논문 {sum(1 for v in refs_by_cn.values() if not v)}편)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="KCI ID 없는 코퍼스 논문의 ScienceON 참고문헌 사전 저장")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--export", metavar="PATH", help="받은 결과를 파일로만 저장")
    parser.add_argument("--import", dest="import_path", metavar="PATH", help="파일을 DB에 저장 (ScienceON 호출 없음)")
    args = parser.parse_args()

    if args.import_path:
        refs_by_cn = json.loads(Path(args.import_path).read_text(encoding="utf-8"))
        print(f"[파일] {len(refs_by_cn)}편")
        if not args.dry_run:
            await _store(refs_by_cn)
        return

    targets = await _targets()
    print(f"[대상] KCI ID 없는 코퍼스 국내 학술지 논문 {len(targets)}편")
    if args.dry_run:
        return
    refs_by_cn = await _fetch(targets)
    if args.export:
        Path(args.export).write_text(json.dumps(refs_by_cn, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[파일] {args.export} — {len(refs_by_cn)}편 / {sum(len(v) for v in refs_by_cn.values())}행")
        return
    await _store(refs_by_cn)


if __name__ == "__main__":
    asyncio.run(main())
