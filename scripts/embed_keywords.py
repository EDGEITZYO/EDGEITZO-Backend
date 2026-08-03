"""Neo4j Keyword 노드 임베딩 후 ChromaDB(keywords 컬렉션) 적재 스크립트

키워드맵 검색(search_keywords)에서 phrase/AND/동의어 사전으로도 못 찾을 때
마지막 fallback으로 쓸 시맨틱 검색용 벡터를 준비한다.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))

import argparse

import chromadb
import torch
from sentence_transformers import SentenceTransformer

from app.core.neo4j_client import get_neo4j_driver  # noqa: E402
from app.core.settings import settings  # noqa: E402

MODEL_NAME = "dragonkue/BGE-m3-ko"
COLLECTION_NAME = "keywords"
BATCH_SIZE = 100


def _select_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _fetch_keyword_nodes() -> list[dict]:
    cypher = """
    MATCH (k:Keyword)
    OPTIONAL MATCH (p:Paper)-[:HAS_KEYWORD]->(k)
    RETURN k.key AS key, k.name AS name, k.lang AS lang, count(DISTINCT p) AS paper_count
    """
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            return [dict(r) for r in session.run(cypher)]
    finally:
        driver.close()


def _batches(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="기존 컬렉션 삭제 후 재생성")
    parser.add_argument("--skip-existing", action="store_true", help="이미 컬렉션에 있는 key는 건너뜀")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()

    print("Neo4j에서 Keyword 노드 조회 중...")
    keywords = _fetch_keyword_nodes()
    keywords = [k for k in keywords if k.get("key") and k.get("name")]
    print(f"조회: {len(keywords)}건")

    device = _select_device()
    print(f"디바이스: {device}")
    print(f"모델 로딩: {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME, device=device)

    print(f"ChromaDB 연결: {settings.chroma_host}:{settings.chroma_port}")
    chroma_client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    chroma_client.heartbeat()
    if args.reset:
        try:
            chroma_client.delete_collection(COLLECTION_NAME)
            print(f"Collection '{COLLECTION_NAME}' 기존 데이터 삭제 완료")
        except Exception:
            pass
    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"Collection '{COLLECTION_NAME}' 준비 완료")

    if args.skip_existing:
        existing_ids = set(collection.get(include=[])["ids"])
        before_count = len(keywords)
        keywords = [k for k in keywords if k["key"] not in existing_ids]
        print(f"기존 항목 건너뜀: {before_count - len(keywords)}건 / 남은 적재: {len(keywords)}건")

    if not keywords:
        print("적재할 신규 키워드가 없습니다.")
        print(f"Collection 총 문서 수: {collection.count()}건")
        return

    # BGE-m3-ko 권장: 문서(=검색 대상) 임베딩 시 "passage: " prefix
    texts = [f"passage: {k['name']}" for k in keywords]
    ids = [k["key"] for k in keywords]
    metadatas = [
        {"key": k["key"], "name": k["name"], "lang": k["lang"] or "", "paper_count": k["paper_count"]}
        for k in keywords
    ]

    start = time.time()
    total_upserted = 0

    for batch_ids, batch_texts, batch_metas in zip(
        _batches(ids, args.batch_size), _batches(texts, args.batch_size), _batches(metadatas, args.batch_size)
    ):
        embeddings = model.encode(
            batch_texts,
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
        ).tolist()
        collection.upsert(
            ids=batch_ids,
            documents=batch_texts,
            embeddings=embeddings,
            metadatas=batch_metas,
        )
        total_upserted += len(batch_ids)
        print(f"  적재 중... 누적 {total_upserted}/{len(keywords)}건")

    elapsed = time.time() - start
    print("\n=== 적재 완료 ===")
    print(f"총 적재: {total_upserted}건")
    print(f"소요 시간: {elapsed:.1f}초")
    print(f"Collection 총 문서 수: {collection.count()}건")


if __name__ == "__main__":
    main()
