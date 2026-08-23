"""researchers 테이블의 임베딩을 ChromaDB 'researchers' 컬렉션으로 이관.

embed_researchers.py --to-chroma 와 결과가 같지만, BGE 모델을 다시 돌리지 않고
Postgres에 이미 저장된 embedding 컬럼을 그대로 밀어넣는다.

왜 필요한가:
  ChromaDB는 로컬 Docker 볼륨이라 배포 시 서버로 넘어가지 않는다. 서버에서
  embed_researchers.py를 다시 돌리면 CPU 인코딩으로 10~15분이 걸리고, 모델
  버전이나 입력 텍스트가 조금이라도 다르면 로컬과 다른 벡터가 만들어진다.
  이 스크립트는 Postgres 덤프로 이미 넘어간 벡터를 재사용하므로 몇 초면 끝나고
  로컬과 완전히 동일한 벡터가 보장된다 (Postgres가 단일 진실 공급원).

전제:
  researchers.embedding / embedding_text 가 채워져 있어야 한다
  (embed_researchers.py를 로컬에서 실행한 뒤 덤프를 복원한 상태).

사용법:
  python scripts/sync_researchers_to_chroma.py --dry-run
  python scripts/sync_researchers_to_chroma.py
  python scripts/sync_researchers_to_chroma.py --reset   # 컬렉션 삭제 후 재생성
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

import chromadb
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.settings import settings
from app.models.researcher import Researcher

COLLECTION_NAME = "researchers"  # embed_researchers.py와 동일해야 함
BATCH_SIZE = 200


async def _fetch_rows() -> list[tuple[str, str, list[float], str]]:
    """(researcher_id, name, embedding, embedding_text) — 임베딩 있는 행만."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                Researcher.researcher_id,
                Researcher.author_name_kor,
                Researcher.author_name_eng,
                Researcher.embedding,
                Researcher.embedding_text,
            ).where(Researcher.embedding.isnot(None))
        )
        rows = []
        for rid, name_ko, name_en, emb, text in result.all():
            if not emb:
                continue
            rows.append((rid, name_ko or name_en or "", list(emb), text or ""))
        return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="researchers 임베딩 → ChromaDB 이관")
    parser.add_argument("--dry-run", action="store_true", help="Chroma 쓰기 없이 건수만 확인")
    parser.add_argument("--reset", action="store_true", help="기존 컬렉션 삭제 후 재생성")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    rows = asyncio.run(_fetch_rows())
    if not rows:
        print("[오류] embedding이 채워진 researchers 행이 없습니다. "
              "덤프 복원 또는 embed_researchers.py 실행이 선행돼야 합니다.", file=sys.stderr)
        sys.exit(1)

    dim = len(rows[0][2])
    print(f"[postgres] 임베딩 보유 연구자 {len(rows):,}명 (차원 {dim})")

    if args.dry_run:
        print("[dry-run] Chroma 쓰기 스킵")
        return

    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    client.heartbeat()

    if args.reset:
        try:
            client.delete_collection(COLLECTION_NAME)
            print(f"[reset]    '{COLLECTION_NAME}' 컬렉션 삭제")
        except Exception:
            pass  # 없으면 그냥 생성으로 진행

    # embed_researchers.push_to_chroma와 동일한 설정 — 코사인 거리
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )

    for start in range(0, len(rows), args.batch_size):
        chunk = rows[start : start + args.batch_size]
        collection.upsert(
            ids=[r[0] for r in chunk],
            embeddings=[r[2] for r in chunk],
            documents=[r[3] for r in chunk],
            metadatas=[{"name": r[1]} for r in chunk],
        )
        print(f"  upserted {min(start + len(chunk), len(rows)):,}/{len(rows):,}")

    print(f"[done]     '{COLLECTION_NAME}' 컬렉션 {collection.count():,}건")


if __name__ == "__main__":
    main()
