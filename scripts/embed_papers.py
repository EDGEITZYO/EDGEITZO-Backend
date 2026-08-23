"""ScienceON 논문 임베딩 후 ChromaDB 적재 스크립트"""
from __future__ import annotations

import json
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

from app.core.settings import settings  # noqa: E402

# 코퍼스 단일 소스. scienceon_preprocessed.json을 1순위로 자동 탐색하던 로직을 없앴다 —
# 그 파일은 어느 환경에도 생성된 적이 없어 늘 폴백만 탔고, 생성 스크립트
# (preprocess_corpus.py)가 Abstract를 형태소 어간 + TF-IDF 절삭 토큰 나열로 바꿔놓아
# 자연어 문장으로 학습된 BGE-m3에는 오히려 해롭다. 전처리본을 쓰려면 --input 으로 명시할 것.
DEFAULT_INPUT_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"
MODEL_NAME = "dragonkue/BGE-m3-ko"
COLLECTION_NAME = "papers"
BATCH_SIZE = 100

# CUDA 있으면 cuda, 없으면 cpu 선택 (MPS는 모델 크기로 인한 OOM으로 제외)
def _select_device() -> str:
    # BGE-m3-ko는 모델 크기(~5 GiB)가 커서 MPS 공유 메모리 초과 발생.
    # 일회성 배치 작업이므로 CPU(시스템 RAM)로 안정 실행.
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"

# Title(2회) + Abstract + Keyword 리스트를 공백으로 이어 임베딩용 단일 텍스트 생성
# 제목을 2회 반복해 벡터가 제목 의미에 더 집중하도록 가중치 부여
# BGE-m3-ko 권장: 문서 임베딩 시 "passage: " prefix
def _build_text(paper: dict) -> str:
    parts: list[str] = []
    if paper.get("Title"):
        parts.append(paper["Title"])
        parts.append(paper["Title"])  # 제목 가중치
    if paper.get("Abstract"):
        parts.append(paper["Abstract"])
    keywords = paper.get("Keyword")
    if keywords:
        parts.append(" ".join(keywords))
    return f"passage: {' '.join(parts)}"

# ChromaDB 저장용 8개 필드 정제 (None → "", list → ";".join())
# Pubyear는 where절 $gte/$lte 숫자 비교를 위해 int로 저장 (다른 필드는 문자열 유지)
def _build_metadata(paper: dict) -> dict:
    def _str(val) -> str:
        if val is None:
            return ""
        if isinstance(val, list):
            return ";".join(str(v) for v in val)
        return str(val)

    def _int_or_none(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    metadata = {
        "CN":          _str(paper.get("CN")),
        "Title":       _str(paper.get("Title")),
        "DBCode":      _str(paper.get("DBCode")),
        "Author":      _str(paper.get("Author")),
        "JournalName": _str(paper.get("JournalName")),
        "DOI":         _str(paper.get("DOI")),
        "ISSN":        _str(paper.get("ISSN")),
    }

    pubyear = _int_or_none(paper.get("Pubyear"))
    if pubyear is not None:
        metadata["Pubyear"] = pubyear

    return metadata

# 리스트를 size 단위로 잘라 순서대로 yield하는 제너레이터
def _batches(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]

# 모델 로드 → ChromaDB 연결 → 100건 배치 단위로 임베딩 후 upsert, 완료 통계 출력
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="기존 컬렉션 삭제 후 재생성")
    parser.add_argument("--skip-existing", action="store_true", help="이미 컬렉션에 있는 CN은 건너뜀")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="ChromaDB upsert 배치 크기")
    parser.add_argument("--input", default=None, help="입력 JSON 파일 경로 (기본: scienceon_keywords_normalized.json)")
    args = parser.parse_args()

    input_path = Path(args.input) if args.input else DEFAULT_INPUT_PATH
    print(f"[입력] {input_path.name}")

    if not input_path.exists():
        print(f"[오류] 입력 파일 없음: {input_path}")
        sys.exit(1)

    data = json.loads(input_path.read_text(encoding="utf-8"))
    papers: list[dict] = data.get("papers", data) if isinstance(data, dict) else data
    print(f"입력: {len(papers)}건 로드")

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
        before_count = len(papers)
        papers = [p for p in papers if p.get("CN") not in existing_ids]
        print(f"기존 문서 건너뜀: {before_count - len(papers)}건 / 남은 적재: {len(papers)}건")

    if not papers:
        print("적재할 신규 문서가 없습니다.")
        print(f"Collection 총 문서 수: {collection.count()}건")
        return

    texts = [_build_text(p) for p in papers]
    ids = [p["CN"] for p in papers]
    metadatas = [_build_metadata(p) for p in papers]

    start = time.time()
    total_upserted = 0

    for i, (batch_papers, batch_ids, batch_texts, batch_metas) in enumerate(
        zip(
            _batches(papers, args.batch_size),
            _batches(ids, args.batch_size),
            _batches(texts, args.batch_size),
            _batches(metadatas, args.batch_size),
        )
    ):
        batch_num = i + 1
        print(f"  배치 {batch_num}: {len(batch_ids)}건 임베딩 중...", end=" ", flush=True)
        embeddings = model.encode(
            batch_texts,
            batch_size=8,
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
        print(f"완료 (누적 {total_upserted}건)")

    elapsed = time.time() - start
    print(f"\n=== 적재 완료 ===")
    print(f"총 적재: {total_upserted}건")
    print(f"소요 시간: {elapsed:.1f}초")
    print(f"Collection 총 문서 수: {collection.count()}건")


if __name__ == "__main__":
    main()
