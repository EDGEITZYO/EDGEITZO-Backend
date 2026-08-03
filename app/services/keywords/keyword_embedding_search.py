"""키워드 임베딩(BGE-m3-ko) 기반 시맨틱 검색.

search_keywords()의 phrase/AND/동의어 사전 매칭이 모두 실패했을 때 마지막 fallback으로 쓴다.
scripts/embed_keywords.py로 미리 Chroma 'keywords' 컬렉션에 전체 키워드를 임베딩해둬야 한다.

임계값(_SIMILARITY_THRESHOLD)은 실측 기반: 코퍼스와 무관한 질의는 최고 유사도가 대체로 0.5~0.6,
의미상 진짜 유사한 질의는 0.68~0.78 사이였음 (완전히 존재하지 않는 개념/무관한 도메인 6~7개 vs
동의어·paraphrase 6~7개로 확인). 그 사이인 0.65로 설정해 애매한 건 결과 없음 처리한다.
"""
from __future__ import annotations

import logging
from typing import Optional

import chromadb

from app.core.settings import settings
from app.services.embedding_model import get_bge_model

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "keywords"
_SIMILARITY_THRESHOLD = 0.65

_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        _collection = client.get_collection(_COLLECTION_NAME)
    return _collection


def embedding_search(query: str, lang: Optional[str] = None, limit: int = 5) -> list[dict]:
    """의미 기반 키워드 검색. 임계값 미만인 결과는 제외하고, 실패 시 빈 리스트를 반환한다
    (Chroma 미기동 등 인프라 문제로 전체 요청이 죽지 않도록 조용히 폴백)."""
    try:
        model = get_bge_model()
        collection = _get_collection()
        vector = model.encode(f"query: {query}", convert_to_numpy=True).tolist()
        where = {"lang": lang} if lang else None
        result = collection.query(query_embeddings=[vector], n_results=limit, where=where)
    except Exception:
        logger.warning("키워드 임베딩 검색 실패", exc_info=True)
        return []

    metadatas = result["metadatas"][0] if result["metadatas"] else []
    distances = result["distances"][0] if result["distances"] else []

    matches = []
    for meta, dist in zip(metadatas, distances):
        similarity = 1 - dist
        if similarity < _SIMILARITY_THRESHOLD:
            continue
        matches.append(meta)
    return matches
