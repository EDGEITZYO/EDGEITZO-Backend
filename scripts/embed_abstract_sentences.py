"""논문 초록 문장 단위 임베딩 사전계산 스크립트.

목적: 검색 결과 카드의 matched_snippet(검색어와 가장 유사한 문장 하이라이트)을 고를 때,
      chroma_search_service.py가 매 검색 요청마다 후보 논문들의 초록 문장을 실시간으로
      다시 인코딩하고 있었음(실측: 후보 150건에 약 50초 — 후보를 늘릴수록 응답이 느려짐).
      문서 전체 임베딩은 이미 이렇게(사전계산 후 Chroma에 저장) 하고 있는데 문장 단위만
      실시간 계산이었던 게 원인. 이 스크립트로 전체 논문의 초록 문장 임베딩을 한 번만
      계산해 로컬 캐시 파일로 저장해두면, 서비스는 저장된 벡터를 불러와 코사인 유사도만
      계산하면 되므로(모델 추론 없음) 후보 수와 무관하게 항상 빠르다.

실행: python scripts/embed_abstract_sentences.py
      (논문 데이터나 임베딩 모델이 바뀌면 다시 실행해서 캐시 갱신)
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env")

import numpy as np
from sentence_transformers import SentenceTransformer

from app.services.chroma_search_service import (
    _SENTENCE_CACHE_PATH,
    _load_papers,
    _split_sentences,
)
from app.services.embedding_model import MODEL_NAME

BATCH_SIZE = 16  # 운영 서버(vCPU 2 / RAM 3.7GB)에서 256이면 스와핑으로 응답 불가 상태까지 감. 실측 확인됨.


def main() -> None:
    print("논문 데이터 로딩 중...")
    _, papers = _load_papers()
    print(f"{len(papers)}건 로드됨")

    doc_sentences: dict[str, list[str]] = {}
    for paper in papers:
        paper_id = paper.get("CN")
        if not paper_id:
            continue
        abstract = paper.get("Abstract_original") or paper.get("Abstract")
        sentences = _split_sentences(abstract) if abstract else []
        if sentences:
            doc_sentences[paper_id] = sentences

    total_sentences = sum(len(s) for s in doc_sentences.values())
    print(f"초록 있는 논문 {len(doc_sentences)}건, 총 문장 {total_sentences}개")

    print(f"모델 로딩: {MODEL_NAME} ...")
    model = SentenceTransformer(MODEL_NAME, device="cpu")

    # chroma_search_service._batch_best_snippets의 기존 실시간 인코딩과 동일하게
    # prefix 없이 인코딩 — 검색 결과의 스니펫 선택 결과가 이 캐시 도입 전후로 달라지지 않도록.
    flat_sentences: list[str] = []
    owners: list[str] = []
    for paper_id, sentences in doc_sentences.items():
        flat_sentences.extend(sentences)
        owners.extend([paper_id] * len(sentences))

    t0 = time.time()
    all_vecs = model.encode(
        flat_sentences,
        convert_to_numpy=True,
        batch_size=BATCH_SIZE,
        show_progress_bar=True,
    )
    print(f"인코딩 완료: {round(time.time() - t0, 1)}초")

    cache: dict[str, tuple[list[str], np.ndarray]] = {}
    idx = 0
    for paper_id, sentences in doc_sentences.items():
        n = len(sentences)
        cache[paper_id] = (sentences, all_vecs[idx: idx + n])
        idx += n

    _SENTENCE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_SENTENCE_CACHE_PATH, "wb") as f:
        pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"캐시 저장 완료: {_SENTENCE_CACHE_PATH} ({_SENTENCE_CACHE_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
