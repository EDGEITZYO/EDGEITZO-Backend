"""논문별 저자 역할 적재 — KCI articleDetail의 author-division·author-part (마이그레이션 041).

공저자 페이지(함께 작성한 논문)가 저자·공저자의 역할(제1/교신/공동 교신/참여)을 보여주는 데 쓴다.
KCI 이력(researcher_external_papers)은 articleSearch로 받아 역할이 없다.

범위:
  --scope coauthored  우리 연구자 2명 이상이 함께 쓴 논문(기본값). 2026-11-06 기준 20,299편
  --scope all         연구자 논문 전부(KCI ID가 있는 것). 같은 날 기준 67,735편

이미 받은 논문(kci_article_author_roles에 행이 있는 것)은 건너뛰므로 끊겨도 다시 돌리면 이어서 받는다.

연구자를 새로 적재한 뒤 이 스크립트를 꼭 돌릴 필요는 없다 — 함께 작성한 논문 API가 역할 없는 논문을 만나면
그 자리에서 받아 저장한다(researcher_detail_service._fetch_missing_roles, 한 요청 20편까지). 이 스크립트는
많은 양을 미리 채워 첫 조회를 빠르게 하고 싶을 때 쓴다.
네트워크 오류로 끝내 못 받은 논문은 행을 남기지 않는다 — 다음 실행에서 다시 시도한다.

사용법:
  python scripts/load_kci_author_roles.py                 # 공저 논문 전부
  python scripts/load_kci_author_roles.py --limit 100     # 앞에서 100편만(점검용)
  python scripts/load_kci_author_roles.py --status        # 진행 상황만
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_researchers import KCI_PACING, _retry  # noqa: E402 — .env를 먼저 읽는 모듈

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.integrations.kci.researcher_client import KCIResearcherClient  # noqa: E402
from app.services.researcher_detail_service import UPSERT_AUTHOR_ROLES_SQL, author_roles_row  # noqa: E402

# 몇 편마다 커밋할지. 끊겨도 잃는 양이 이 정도다.
COMMIT_EVERY = 50

# 연구자 논문 한 편 = (연구자, 논문 키, KCI ID). 상세페이지 논문 리스트(_PAPERS_SQL)와 같은 두 출처다.
_RESEARCHER_ARTICLES = """
SELECT researcher_id, coalesce(internal_paper_id, external_id) AS paper_key, external_id AS art_id
FROM researcher_external_papers
UNION
SELECT rp.researcher_id, rp.paper_id, p.kci_art_id
FROM researcher_papers rp JOIN papers p ON p.id = rp.paper_id
"""

_TARGETS_SQL = {
    "coauthored": f"""
        WITH ra AS ({_RESEARCHER_ARTICLES})
        SELECT max(art_id) AS art_id FROM ra
        GROUP BY paper_key
        HAVING count(DISTINCT researcher_id) >= 2 AND max(art_id) LIKE 'ART%%'
    """,
    "all": f"""
        WITH ra AS ({_RESEARCHER_ARTICLES})
        SELECT DISTINCT art_id FROM ra WHERE art_id LIKE 'ART%%'
    """,
}



async def _targets(scope: str) -> tuple[list[str], int]:
    async with AsyncSessionLocal() as session:
        wanted = [r.art_id for r in (await session.execute(text(_TARGETS_SQL[scope]))).all()]
        done = {r.art_id for r in (await session.execute(text("SELECT art_id FROM kci_article_author_roles"))).all()}
    todo = sorted(a for a in wanted if a not in done)
    return todo, len(wanted)


async def status(scope: str) -> None:
    todo, total = await _targets(scope)
    print(f"[{scope}] 대상 {total:,}편 / 적재 {total - len(todo):,}편 / 남음 {len(todo):,}편")
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS papers, "
                    "  count(*) FILTER (WHERE cardinality(author_names) = 0) AS empty, "
                    "  count(*) FILTER (WHERE '교신' = ANY(author_parts)) AS has_corr, "
                    "  count(*) FILTER (WHERE '1' = ANY(author_divisions)) AS has_first "
                    "FROM kci_article_author_roles"
                )
            )
        ).first()
    print(
        f"  적재 행 {row.papers:,} — 저자 없음 {row.empty:,} / 제1저자 표시 있음 {row.has_first:,} / "
        f"교신 표시 있음 {row.has_corr:,}"
    )


async def run(scope: str, limit: int | None) -> None:
    todo, total = await _targets(scope)
    if limit:
        todo = todo[:limit]
    print(f"[{scope}] 대상 {total:,}편 중 이번에 받을 논문 {len(todo):,}편")
    if not todo:
        return

    started = time.monotonic()
    saved = failed = 0
    pending: list[dict] = []

    async def flush() -> None:
        if not pending:
            return
        async with AsyncSessionLocal() as session:
            await session.execute(text(UPSERT_AUTHOR_ROLES_SQL), pending)
            await session.commit()
        pending.clear()

    async with httpx.AsyncClient(timeout=30.0) as http:
        client = KCIResearcherClient(http)
        for idx, art_id in enumerate(todo, start=1):
            await asyncio.sleep(KCI_PACING)
            article = await _retry(lambda aid=art_id: client.article_detail(aid), label=f"detail {art_id}")
            if article is None:
                # 응답을 못 받았거나 articleInfo가 없는 응답. 어느 쪽인지 여기서 가를 수 없으니
                # 행을 남기지 않고 다음 실행에 다시 시도한다.
                failed += 1
                continue
            pending.append(author_roles_row(art_id, article))
            saved += 1
            if len(pending) >= COMMIT_EVERY:
                await flush()
            if idx % 500 == 0:
                rate = idx / (time.monotonic() - started)
                remain = (len(todo) - idx) / rate / 60 if rate else 0
                print(f"  {idx:,}/{len(todo):,} — 적재 {saved:,} / 실패 {failed:,} (남은 시간 약 {remain:.0f}분)", flush=True)
    await flush()
    print(f"[{scope}] 완료 — 적재 {saved:,}편 / 실패 {failed:,}편 (다시 실행하면 실패분만 다시 받는다)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scope", choices=sorted(_TARGETS_SQL), default="coauthored")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    asyncio.run(status(args.scope) if args.status else run(args.scope, args.limit))


if __name__ == "__main__":
    main()
