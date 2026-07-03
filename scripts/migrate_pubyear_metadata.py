"""ChromaDB 'papers' 컬렉션의 Pubyear 메타데이터를 문자열 → int로 일괄 변환.

배경: chroma_search_service.py의 연도 필터를 post-retrieval(Python 루프)에서
네이티브 where절 pre-filter($gte/$lte)로 전환하기 위한 선행 작업. 재적재(re-ingest) 없이
collection.update()로 Pubyear 필드만 갱신한다 (다른 필드는 병합되어 보존됨 — 사전 테스트로 확인됨).

Pubyear가 없거나 int 변환 불가능한 문서는 필드를 생략한 상태로 둔다(update 스킵) —
SCI 축과 동일하게 "데이터 없으면 자연 탈락"시키기 위함. collection.update()는 필드를
추가/덮어쓸 수는 있어도 기존 필드를 삭제할 수는 없으므로, 애초에 없던 필드는 그대로
없는 채로 남긴다.

기본은 dry-run이며 --apply를 줘야 실제로 update가 수행된다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import chromadb  # noqa: E402

from app.core.settings import settings  # noqa: E402

_COLLECTION_NAME = "papers"
_BATCH_SIZE = 100


def _int_or_none(val) -> int | None:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _batches(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 update 실행 (없으면 dry-run)")
    parser.add_argument("--batch-size", type=int, default=_BATCH_SIZE)
    args = parser.parse_args()

    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    collection = client.get_collection(_COLLECTION_NAME)

    total = collection.count()
    print(f"컬렉션 총 문서 수: {total}건")

    all_docs = collection.get(include=["metadatas"])
    ids = all_docs["ids"]
    metadatas = all_docs["metadatas"]

    to_update_ids: list[str] = []
    to_update_metas: list[dict] = []
    already_int = 0
    skip_invalid: list[tuple[str, object]] = []

    for doc_id, meta in zip(ids, metadatas):
        raw = meta.get("Pubyear")
        if isinstance(raw, int):
            already_int += 1
            continue
        parsed = _int_or_none(raw)
        if parsed is None:
            skip_invalid.append((doc_id, raw))
            continue
        to_update_ids.append(doc_id)
        to_update_metas.append({"Pubyear": parsed})

    print(f"이미 int: {already_int}건")
    print(f"str → int 변환 대상: {len(to_update_ids)}건")
    print(f"변환 불가(필드 생략, 스킵): {len(skip_invalid)}건")
    for doc_id, raw in skip_invalid[:20]:
        print(f"  - {doc_id}: Pubyear={raw!r}")

    if not args.apply:
        print("\n[dry-run] --apply 없이 실행됨 — 실제 update 수행 안 함")
        return

    if not to_update_ids:
        print("\n변환 대상 없음, 종료")
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
        print(f"  {doc_id}: Pubyear={meta.get('Pubyear')!r} ({type(meta.get('Pubyear')).__name__}), "
              f"DBCode={meta.get('DBCode')!r}, Title 존재={'Title' in meta}")


if __name__ == "__main__":
    main()
