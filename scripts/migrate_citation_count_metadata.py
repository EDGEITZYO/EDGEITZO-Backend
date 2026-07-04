"""ChromaDB 'papers' 컬렉션에 citation_count 메타데이터 신규 추가.

판정 기준 (Pubyear/SCI 축과 동일한 "데이터 없으면 자연 탈락" 원칙):
  - JAKO + KCI 인용수 보강 체크포인트(status=ok): Postgres papers.citation_count 값을 그대로 채움
    (0이어도 KCI가 확인해준 진짜 값이므로 채움)
  - JAKO + unmatched/체크포인트 미기록: 필드 생략 (값 없음, Postgres의 기본값 0을 신뢰하지 않음)
  - 非JAKO(DIKO/JAFO/CFKO): 필드 생략 (애초에 KCI 보강 대상이 아님)

기본은 dry-run이며 --apply를 줘야 실제로 update가 수행된다.
재실행해도 안전(각 실행마다 Postgres+체크포인트+ChromaDB 현재 상태에서 매번 새로 판정).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import chromadb  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.settings import settings  # noqa: E402
from app.models.paper import Paper  # noqa: E402

_COLLECTION_NAME = "papers"
_BATCH_SIZE = 100
_CHECKPOINT_PATH = _PROJECT_ROOT / "data" / "checkpoints" / "kci_citations.json"


def _batches(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


async def _fetch_postgres_citation_counts(cns: list[str]) -> dict[str, int]:
    """scienceon_cn -> citation_count 매핑 (papers 테이블 IN 쿼리, 500건씩 배치)"""
    result: dict[str, int] = {}
    async with AsyncSessionLocal() as session:
        for batch in _batches(cns, 500):
            rows = await session.execute(
                select(Paper.scienceon_cn, Paper.citation_count).where(Paper.scienceon_cn.in_(batch))
            )
            for cn, cc in rows.all():
                if cn is not None:
                    result[cn] = cc
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 update 실행 (없으면 dry-run)")
    parser.add_argument("--batch-size", type=int, default=_BATCH_SIZE)
    args = parser.parse_args()

    checkpoint = (
        json.loads(_CHECKPOINT_PATH.read_text(encoding="utf-8"))
        if _CHECKPOINT_PATH.exists()
        else {}
    )

    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    collection = client.get_collection(_COLLECTION_NAME)

    total = collection.count()
    print(f"컬렉션 총 문서 수: {total}건")

    all_docs = collection.get(include=["metadatas"])
    ids = all_docs["ids"]
    metadatas = all_docs["metadatas"]

    jako_ids = [doc_id for doc_id, meta in zip(ids, metadatas) if meta.get("DBCode") == "JAKO"]
    print(f"JAKO 건수: {len(jako_ids)}건")

    pg_citations = asyncio.run(_fetch_postgres_citation_counts(jako_ids))
    print(f"Postgres에서 조회된 JAKO citation_count 행 수: {len(pg_citations)}건")

    to_update_ids: list[str] = []
    to_update_metas: list[dict] = []
    skip_non_jako = 0
    skip_unmatched = 0

    for doc_id, meta in zip(ids, metadatas):
        if meta.get("DBCode") != "JAKO":
            skip_non_jako += 1
            continue
        status = (checkpoint.get(doc_id) or {}).get("status")
        if status != "ok":
            skip_unmatched += 1
            continue
        cc = pg_citations.get(doc_id)
        if cc is None:
            # Postgres에 없는 경우(이론상 발생 안 해야 함) — 안전하게 생략
            skip_unmatched += 1
            continue
        to_update_ids.append(doc_id)
        to_update_metas.append({"citation_count": int(cc)})

    print()
    print(f"채움 대상(JAKO+status=ok): {len(to_update_ids)}건")
    print(f"생략(非JAKO): {skip_non_jako}건")
    print(f"생략(JAKO+unmatched/미기록/Postgres 미존재): {skip_unmatched}건")
    print(f"합계 검증: {len(to_update_ids)} + {skip_non_jako} + {skip_unmatched} = "
          f"{len(to_update_ids) + skip_non_jako + skip_unmatched} (전체 {total}건과 비교)")

    if not args.apply:
        print("\n[dry-run] --apply 없이 실행됨 — 실제 update 수행 안 함")
        return

    if not to_update_ids:
        print("\n채울 대상 없음, 종료")
        return

    print(f"\n[apply] {len(to_update_ids)}건 update 시작 (배치 {args.batch_size})")
    done = 0
    for batch_ids, batch_metas in zip(
        _batches(to_update_ids, args.batch_size),
        _batches(to_update_metas, args.batch_size),
    ):
        collection.update(ids=batch_ids, metadatas=batch_metas)
        done += len(batch_ids)
        print(f"  {done}/{len(to_update_ids)} 완료")

    print("\n=== 검증 ===")
    sample_ids = to_update_ids[:5]
    verify = collection.get(ids=sample_ids, include=["metadatas"])
    for doc_id, meta in zip(verify["ids"], verify["metadatas"]):
        print(f"  {doc_id}: citation_count={meta.get('citation_count')!r} "
              f"({type(meta.get('citation_count')).__name__}), "
              f"DBCode={meta.get('DBCode')!r}, Title 존재={'Title' in meta}")


if __name__ == "__main__":
    main()
