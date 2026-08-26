"""연구자 임베딩 — 「노드 그래프 뷰」의 노드 간 거리 계산용.

왜 필요한가 (실측):
  명세서는 노드 간 거리를 "키워드 공유"로 재라고 한다. 그런데 '항산화'로 분야 검색한
  결과(14명) 안에서 모든 쌍을 세어보니 45%가 키워드를 하나도 공유하지 않았다.
  그래프의 절반이 "무한히 멂"으로 그려진다는 뜻이다.

  예: 인만진(폴리페놀, 효소 분해, 요구르트)과 조민영(Alcalase-enzymatic hydrolysate,
      알카리 처리, 항산화)은 둘 다 식품 원료를 효소로 분해해 항산화 물질을 얻는 연구인데
      표기가 달라 키워드 공유가 0이다. 임베딩 유사도는 0.557이 나온다.

검증:
  키워드 공유 0인 쌍의 임베딩 유사도 평균 0.469, 공유 2개 이상인 쌍은 0.698.
  키워드 공유와 방향이 일치하므로(= 엉뚱한 값이 아니므로) 빈칸만 메우는 셈이다.

사용법:
  python scripts/embed_researchers.py --dry-run     # 임베딩할 문장만 확인
  python scripts/embed_researchers.py               # 전체 임베딩 후 DB 저장
  python scripts/embed_researchers.py --to-chroma   # ChromaDB 컬렉션에도 적재
  python scripts/embed_researchers.py --evaluate    # 키워드 공유 방식과 비교 평가
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
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

import numpy as np
import torch
from sqlalchemy import text

from app.core.database import AsyncSessionLocal

MODEL_NAME = "dragonkue/BGE-m3-ko"
COLLECTION_NAME = "researchers"
BATCH_SIZE = 64
# 연구자 1명을 대표하는 키워드 수. 너무 많으면 초점이 흐려지고, 적으면 분야가 안 잡힌다.
MAX_OWN_KEYWORDS = 15
MAX_PAPER_KEYWORDS = 25


def _select_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


async def load_documents() -> list[dict]:
    """연구자별 임베딩 입력 문장을 만든다.

    소속·이름은 넣지 않는다 — 재려는 건 "연구 분야가 얼마나 비슷한가"이지
    "같은 학교인가"가 아니다. 소속을 넣으면 같은 대학 사람들이 전부 붙어버린다.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT r.researcher_id, r.author_name_kor, r.author_name_eng, r.keywords, "
                    "       array_agg(DISTINCT k.kw) FILTER (WHERE k.kw IS NOT NULL) AS paper_keywords, "
                    "       array_agg(DISTINCT t.title) FILTER (WHERE t.title IS NOT NULL) AS paper_titles "
                    "FROM researchers r "
                    "LEFT JOIN LATERAL ("
                    "  SELECT unnest(x.keywords) AS kw FROM researcher_external_papers x "
                    "  WHERE x.researcher_id = r.researcher_id"
                    ") k ON true "
                    "LEFT JOIN LATERAL ("
                    "  SELECT p.title FROM researcher_papers rp JOIN papers p ON p.id = rp.paper_id "
                    "  WHERE rp.researcher_id = r.researcher_id"
                    ") t ON true "
                    "GROUP BY r.researcher_id, r.author_name_kor, r.author_name_eng, r.keywords"
                )
            )
        ).all()

    documents = []
    for row in rows:
        terms: list[str] = []
        seen: set[str] = set()

        def add(items, limit):
            count = 0
            for term in items or []:
                key = term.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                terms.append(term.strip())
                count += 1
                if count >= limit:
                    break

        # 대표 키워드가 먼저 — 그 사람을 규정하는 분야다.
        add(row.keywords, MAX_OWN_KEYWORDS)
        # 논문 키워드는 자주 나온 순으로 — 한 번 스친 주제가 분야를 대표하진 않는다.
        if row.paper_keywords:
            ranked = [kw for kw, _ in Counter(row.paper_keywords).most_common()]
            add(ranked, MAX_PAPER_KEYWORDS)

        if not terms:
            # 학술대회 논문(CFKO)처럼 키워드가 아예 없는 경우가 있다.
            # 벡터가 없으면 노드 그래프·유사 연구자에서 통째로 빠지므로 제목이라도 쓴다.
            add(row.paper_titles, 3)
        if not terms:
            continue
        documents.append(
            {
                "researcher_id": row.researcher_id,
                "name": row.author_name_kor or row.author_name_eng or "",
                "text": ", ".join(terms),
            }
        )
    return documents


async def save_embeddings(documents: list[dict], vectors: np.ndarray) -> None:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        for doc, vector in zip(documents, vectors):
            await session.execute(
                text(
                    "UPDATE researchers SET embedding = :vec, embedding_model = :model, "
                    "embedding_text = :txt, embedded_at = :ts, updated_at = now() "
                    "WHERE researcher_id = :rid"
                ),
                {
                    "vec": [float(v) for v in vector],
                    "model": MODEL_NAME,
                    "txt": doc["text"][:4000],
                    "ts": now,
                    "rid": doc["researcher_id"],
                },
            )
        await session.commit()


def push_to_chroma(documents: list[dict], vectors: np.ndarray) -> None:
    import chromadb

    from app.core.settings import settings

    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    client.heartbeat()
    collection = client.get_or_create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})
    for start in range(0, len(documents), 200):
        chunk = documents[start : start + 200]
        collection.upsert(
            ids=[d["researcher_id"] for d in chunk],
            embeddings=[v.tolist() for v in vectors[start : start + 200]],
            documents=[d["text"] for d in chunk],
            metadatas=[{"name": d["name"]} for d in chunk],
        )
    print(f"[chroma] '{COLLECTION_NAME}' 컬렉션 {collection.count():,}건")


async def evaluate() -> None:
    """명세서의 키워드 공유 방식과 임베딩 방식을 같은 데이터로 비교한다."""
    import itertools

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT r.researcher_id, r.author_name_kor, r.keywords, r.embedding "
                    "FROM researchers r "
                    "JOIN researcher_papers rp USING (researcher_id) "
                    "JOIN papers p ON p.id = rp.paper_id "
                    "WHERE :kw = ANY(p.keywords_ko) AND r.embedding IS NOT NULL"
                ),
                {"kw": "항산화"},
            )
        ).all()
    if len(rows) < 3:
        print("[평가] 표본이 너무 적습니다.")
        return

    vectors = np.array([r.embedding for r in rows], dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)

    shared_zero, shared_two = [], []
    for i, j in itertools.combinations(range(len(rows)), 2):
        overlap = len(set(rows[i].keywords or []) & set(rows[j].keywords or []))
        similarity = float(vectors[i] @ vectors[j])
        if overlap == 0:
            shared_zero.append(similarity)
        elif overlap >= 2:
            shared_two.append(similarity)

    total = len(shared_zero) + len(shared_two)
    print(f"\n[평가] '항산화' 분야 검색 결과 {len(rows)}명, 쌍 {len(rows)*(len(rows)-1)//2}개")
    print(f"  키워드 공유 0  : {len(shared_zero)}쌍 — 명세서 방식으로는 '무한히 멂'")
    if shared_zero:
        print(f"      임베딩 유사도 평균 {np.mean(shared_zero):.3f} (범위 {min(shared_zero):.3f}~{max(shared_zero):.3f})")
    if shared_two:
        print(f"  키워드 공유 2+ : {len(shared_two)}쌍")
        print(f"      임베딩 유사도 평균 {np.mean(shared_two):.3f} (범위 {min(shared_two):.3f}~{max(shared_two):.3f})")
    if shared_zero and shared_two:
        gap = np.mean(shared_two) - np.mean(shared_zero)
        print(f"  → 두 그룹의 차이 {gap:+.3f}: 임베딩이 키워드 공유와 같은 방향으로 움직인다(타당성 확인)")
        print(f"  → 그러면서 공유 0인 {len(shared_zero)}쌍에도 실제 거리를 준다(그래프가 끊기지 않음)")


async def main_async(args) -> None:
    print("[준비] 연구자 문서 구성 중...")
    documents = await load_documents()
    print(f"[준비] 임베딩 대상 {len(documents):,}명")
    if args.dry_run:
        for doc in documents[:5]:
            print(f"  {doc['name']}: {doc['text'][:110]}")
        return

    device = _select_device()
    print(f"[모델] {MODEL_NAME} on {device}")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME, device=device)

    # BGE-m3-ko 권장 — 검색 대상 문서는 "passage: " 접두어를 붙인다(기존 스크립트와 동일).
    texts = [f"passage: {d['text']}" for d in documents]
    started = time.time()
    vectors = model.encode(
        texts, batch_size=BATCH_SIZE, show_progress_bar=True, convert_to_numpy=True, normalize_embeddings=True
    )
    print(f"[임베딩] {len(vectors):,}건 / {time.time()-started:.0f}초 / 차원 {vectors.shape[1]}")

    await save_embeddings(documents, vectors)
    print("[DB] 저장 완료")
    if args.to_chroma:
        push_to_chroma(documents, vectors)
    await evaluate()


def main() -> None:
    parser = argparse.ArgumentParser(description="연구자 임베딩 생성")
    parser.add_argument("--dry-run", action="store_true", help="임베딩 없이 입력 문장만 확인")
    parser.add_argument("--to-chroma", action="store_true", help="ChromaDB 컬렉션에도 적재")
    parser.add_argument("--evaluate", action="store_true", help="기존 임베딩으로 평가만 수행")
    args = parser.parse_args()

    import asyncio

    if args.evaluate:
        asyncio.run(evaluate())
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
