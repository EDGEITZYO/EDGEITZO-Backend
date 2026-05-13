"""ChromaDB 유사도 검색 테스트 스크립트"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))

import torch
import chromadb
from sentence_transformers import SentenceTransformer

from app.core.settings import settings  # noqa: E402

MODEL_NAME = "dragonkue/BGE-m3-ko"
COLLECTION_NAME = "papers"
TOP_K = 5

QUERIES = [
    "유전자 발현 조절 메커니즘",
    "줄기세포 분화 신호 전달",
    "효소 활성화 단백질 구조",
    "protein folding mechanism",
]


def _select_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def main() -> None:
    device = _select_device()
    print(f"디바이스: {device}")
    print(f"모델 로딩: {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME, device=device)

    print(f"ChromaDB 연결: {settings.chroma_host}:{settings.chroma_port}\n")
    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    collection = client.get_collection(name=COLLECTION_NAME)
    print(f"Collection '{COLLECTION_NAME}': {collection.count()}건\n")

    for query in QUERIES:
        print(f"{'='*60}")
        print(f"쿼리: {query}")
        print(f"{'='*60}")

        query_vec = model.encode(query, convert_to_numpy=True).tolist()
        results = collection.query(
            query_embeddings=[query_vec],
            n_results=TOP_K,
            include=["metadatas", "distances"],
        )

        metadatas = results["metadatas"][0]
        distances = results["distances"][0]

        for rank, (meta, dist) in enumerate(zip(metadatas, distances), start=1):
            score = 1 - dist  # cosine distance → similarity
            title = meta.get("Title", "")[:60]
            print(
                f"  {rank}. [{meta.get('DBCode','?')}] (score={score:.4f})\n"
                f"     CN: {meta.get('CN','?')}\n"
                f"     {title}"
            )
        print()


if __name__ == "__main__":
    main()
