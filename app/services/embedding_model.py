"""BGE-m3-ko 모델 공유 싱글턴.

논문 검색(chroma_search_service)과 키워드 임베딩 검색(keyword_embedding_search)이
같은(~5GiB) 모델을 각자 로드하면 메모리가 이중으로 소모되므로 하나만 로드해 공유한다.
"""
from __future__ import annotations

from sentence_transformers import SentenceTransformer

MODEL_NAME = "dragonkue/BGE-m3-ko"

_model: SentenceTransformer | None = None


def get_bge_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model
