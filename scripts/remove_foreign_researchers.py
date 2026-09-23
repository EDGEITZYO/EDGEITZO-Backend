"""연구자를 국내(한국인) 연구자로만 한정한다 — 해외 연구자를 Postgres에서 제거.

배경:
  코퍼스를 국내 논문 100%로 바꾸면서(scripts/remove_foreign_papers.py) JAFO 150편과
  그 논문으로만 들어온 연구자는 지워졌다. 그런데 **국내 학술지(JAKO) 논문에 공저자로
  올라간 해외 연구자**는 그 조건에 안 걸려 154명이 남았다 — UAE University, Fred Hutch,
  University of Michigan, CSIR(인도), Uva Wellassa University(스리랑카) 등.

판정 기준:
  한글명이 없고(author_name_kor IS NULL) **KCI 논문이 0편**인 연구자를 해외로 본다.

  소속 문자열로 가르면 안 된다 — 'Yonsei University', 'Kyungpook National University'처럼
  국내 기관의 영문 표기가 137건(kci) + 65건(scienceon) 있어서 멀쩡한 국내 연구자가 지워진다.
  반대로 한글명 보유는 국내 연구자의 충분조건에 가깝다(실측: 2,110명 중 1,955명이 보유,
  판정 불가 155명은 전원 KCI 논문 0편).

먼저 확인할 것 — 코퍼스 커버리지:
  삭제 대상만 저자로 달린 코퍼스 논문이 31편 있다. 전부 kci_art_id가 없어서 anchor 경로에
  안 걸렸고 OpenAlex 경로로만 저자가 붙어 있던 논문이다(§11의 DIKO 누락과 같은 구조).
  **이 스크립트 다음에 반드시 load_researchers.py --stage thesis를 돌려야 한다** —
  31편 모두 scienceon_cn이 있어 ScienceON 논문 조회로 국내 저자를 읽어올 수 있고,
  thesis 단계의 "researcher_papers 링크가 없는 코퍼스 논문" 조건에 자동으로 걸린다.

Neo4j는 건드리지 않는다:
  Aura는 로컬·운영이 같은 인스턴스다. 여기서 ResearcherNode를 지우면 운영 화면이 즉시
  어긋난다(운영 Postgres는 아직 옛 데이터). 적재를 모두 끝내고 배포 시점에
  scripts/build_researcher_graph.py --load로 한 번에 맞춘다.

사용법:
  python scripts/remove_foreign_researchers.py              # dry-run (기본)
  python scripts/remove_foreign_researchers.py --apply
  python scripts/remove_foreign_researchers.py --apply --skip-chroma
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
        if _k.strip():
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.settings import settings

# 한글명이 없고 KCI 논문이 0편인 연구자. 두 조건을 모두 만족해야 한다 —
# 한글명이 없어도 KCI에 논문이 있으면 국내 학술지에 낸 사람이라 국내로 본다.
_TARGET_SQL = """
SELECT researcher_id, source, author_name_eng, institution_current, total_citations
FROM researchers r
WHERE r.author_name_kor IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM researcher_external_papers e
      WHERE e.researcher_id = r.researcher_id AND e.external_source = 'kci'
  )
ORDER BY r.total_citations DESC NULLS LAST
"""

# 삭제하면 저자가 0명이 되는 코퍼스 논문. thesis 단계로 복구해야 하는 목록이다.
_ORPHAN_SQL = """
WITH tgt AS (
    SELECT researcher_id FROM researchers r
    WHERE r.author_name_kor IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM researcher_external_papers e
          WHERE e.researcher_id = r.researcher_id AND e.external_source = 'kci'
      )
)
SELECT rp.paper_id
FROM researcher_papers rp
JOIN papers p ON p.id = rp.paper_id
WHERE p.source IN ('knowledge_base', 'kci_reference_expansion')
GROUP BY rp.paper_id
HAVING count(*) FILTER (WHERE rp.researcher_id NOT IN (SELECT researcher_id FROM tgt)) = 0
"""


def _drop_from_chroma(ids: list[str]) -> None:
    """연구자 벡터 컬렉션에서도 뺀다. 없으면 조용히 넘어간다."""
    try:
        import chromadb

        client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        collection = client.get_collection("researchers")
        before = collection.count()
        for i in range(0, len(ids), 200):
            collection.delete(ids=ids[i : i + 200])
        print(f"  [chroma] researchers {before:,} → {collection.count():,}건")
    except Exception as exc:  # 컬렉션 없음·서버 미기동 등
        print(f"  [chroma] 건너뜀 ({type(exc).__name__}: {str(exc)[:80]})")


async def main() -> None:
    parser = argparse.ArgumentParser(description="해외 연구자 제거 (Postgres 전용)")
    parser.add_argument("--apply", action="store_true", help="실제로 삭제한다 (기본은 dry-run)")
    parser.add_argument("--skip-chroma", action="store_true", help="ChromaDB 정리를 건너뛴다")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        targets = (await session.execute(text(_TARGET_SQL))).all()
        orphans = [r.paper_id for r in (await session.execute(text(_ORPHAN_SQL))).all()]

    if not targets:
        print("삭제 대상이 없습니다 (이미 정리됨).")
        return

    ids = [r.researcher_id for r in targets]
    by_source: dict[str, int] = {}
    for row in targets:
        by_source[row.source] = by_source.get(row.source, 0) + 1

    print(f"삭제 대상 {len(targets):,}명 — " + " / ".join(f"{k} {v}" for k, v in sorted(by_source.items())))
    print("\n상위 5명 (피인용순):")
    for row in targets[:5]:
        print(f"  {row.researcher_id}  {row.author_name_eng}  {row.institution_current}  인용 {row.total_citations}")

    print(f"\n저자가 0명이 되는 코퍼스 논문: {len(orphans)}편")
    if orphans:
        print("  → 삭제 후 반드시 실행: python scripts/load_researchers.py --stage thesis")

    if not args.apply:
        print("\n(모의실행) --apply 를 붙이면 실제로 삭제합니다.")
        return

    async with AsyncSessionLocal() as session:
        # researcher_flow_cache는 researchers에 FK가 없어(캐시 테이블) 수동으로 지운다.
        # researcher_papers / researcher_external_papers는 ON DELETE CASCADE로 따라 지워진다.
        cache_deleted = (
            await session.execute(
                text("DELETE FROM researcher_flow_cache WHERE researcher_id = ANY(:ids)"), {"ids": ids}
            )
        ).rowcount
        deleted = (
            await session.execute(
                text("DELETE FROM researchers WHERE researcher_id = ANY(:ids)"), {"ids": ids}
            )
        ).rowcount
        await session.commit()

    print(f"\n삭제 완료: researchers {deleted:,}행 / flow_cache {cache_deleted:,}행")
    if not args.skip_chroma:
        _drop_from_chroma(ids)

    async with AsyncSessionLocal() as session:
        remaining = (await session.execute(text("SELECT count(*) FROM researchers"))).scalar()
        korean = (
            await session.execute(text("SELECT count(*) FROM researchers WHERE author_name_kor IS NOT NULL"))
        ).scalar()
    print(f"남은 연구자 {remaining:,}명 (한글명 보유 {korean:,}명)")


if __name__ == "__main__":
    asyncio.run(main())
