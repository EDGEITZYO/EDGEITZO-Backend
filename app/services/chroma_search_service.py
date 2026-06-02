"""
ChromaDB 시맨틱 + BM25 키워드 하이브리드 검색 서비스

검색 흐름:
  1. BGE-m3-ko로 쿼리 임베딩 → ChromaDB 코사인 유사도 검색 (의미 기반)
  2. BM25Okapi로 키워드 빈도 검색 (단어 매칭 기반)
  3. Reciprocal Rank Fusion(RRF)으로 두 결과 통합 → 최종 랭킹

싱글턴 패턴: 모델/인덱스를 앱 수명 동안 한 번만 초기화
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import chromadb
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from app.core.settings import settings
from app.schemas.search import CredibilityInfo, PaperAuthor, PaperSearchItem

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PREPROCESSED_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_preprocessed.json"
_FALLBACK_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"

_MODEL_NAME = "dragonkue/BGE-m3-ko"
_COLLECTION_NAME = "papers"
_RRF_K = 60  # RRF 상수 — 값이 클수록 하위 랭크 페널티 완화


def _load_papers() -> tuple[dict[str, dict], list[dict]]:
    path = _PREPROCESSED_PATH if _PREPROCESSED_PATH.exists() else _FALLBACK_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    papers: list[dict] = data.get("papers", data) if isinstance(data, dict) else data
    return {p["CN"]: p for p in papers if p.get("CN")}, papers


def _paper_to_bm25_text(paper: dict) -> str:
    parts = []
    if paper.get("Title"):
        parts.append(paper["Title"])
        parts.append(paper["Title"])  # 제목 가중치
    abstract = paper.get("Abstract_original") or paper.get("Abstract") or ""
    if abstract:
        parts.append(abstract)
    keywords = paper.get("Keyword") or []
    if isinstance(keywords, list):
        parts.append(" ".join(keywords))
    return " ".join(parts)


def _tokenize(text: str) -> list[str]:
    return text.lower().split()


def _to_search_item(paper: dict, score: float) -> PaperSearchItem:
    authors_raw = paper.get("Author") or []
    if isinstance(authors_raw, str):
        authors_raw = [a.strip() for a in authors_raw.split(";") if a.strip()]
    authors = [PaperAuthor(name=a) for a in authors_raw]

    keywords = paper.get("Keyword") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(";") if k.strip()]

    year_val = paper.get("Pubyear")
    year = int(year_val) if year_val and str(year_val).isdigit() else None

    issn_val = paper.get("ISSN")
    issn = issn_val[0] if isinstance(issn_val, list) and issn_val else issn_val

    abstract = paper.get("Abstract_original") or paper.get("Abstract")

    return PaperSearchItem(
        paper_id=paper.get("CN", ""),
        title=paper.get("Title", ""),
        authors=authors,
        year=year,
        abstract=abstract,
        keywords=keywords,
        journal_name=paper.get("JournalName"),
        issn=issn,
        doi=paper.get("DOI") or None,
        db_code=paper.get("DBCode"),
        source="local_chroma",
        credibility=CredibilityInfo(badge="unknown"),
        score=round(score, 4),
    )


def _rrf_combine(
    semantic: list[tuple[str, float]],
    bm25: list[tuple[str, float]],
    k: int = _RRF_K,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: 두 랭킹을 1/(k+rank) 점수로 합산해 재정렬"""
    rrf: dict[str, float] = {}
    for rank, (doc_id, _) in enumerate(semantic):
        rrf[doc_id] = rrf.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (doc_id, _) in enumerate(bm25):
        rrf[doc_id] = rrf.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(rrf.items(), key=lambda x: x[1], reverse=True)


class ChromaSearchService:
    def __init__(self) -> None:
        self._model: Optional[SentenceTransformer] = None
        self._collection = None
        self._paper_index: dict[str, dict] = {}
        self._papers: list[dict] = []
        self._bm25: Optional[BM25Okapi] = None
        self._ready = False

    def _init(self) -> None:
        if self._ready:
            return

        self._paper_index, self._papers = _load_papers()

        tokenized = [_tokenize(_paper_to_bm25_text(p)) for p in self._papers]
        self._bm25 = BM25Okapi(tokenized)

        self._model = SentenceTransformer(_MODEL_NAME, device="cpu")

        chroma_client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
        )
        self._collection = chroma_client.get_collection(_COLLECTION_NAME)
        self._ready = True

    def _semantic_search(self, query: str, n: int) -> list[tuple[str, float]]:
        # BGE-m3-ko 권장: 쿼리 임베딩 시 "query: " prefix
        query_vec = self._model.encode(f"query: {query}", convert_to_numpy=True).tolist()
        results = self._collection.query(
            query_embeddings=[query_vec],
            n_results=min(n, self._collection.count()),
        )
        return [
            (doc_id, max(0.0, 1.0 - dist))
            for doc_id, dist in zip(results["ids"][0], results["distances"][0])
        ]

    def _bm25_search(self, query: str, n: int) -> list[tuple[str, float]]:
        tokens = _tokenize(query)
        scores = self._bm25.get_scores(tokens)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n]
        return [(self._papers[i]["CN"], float(scores[i])) for i in top_indices]

    def _sync_search(
        self,
        query: str,
        n_results: int,
        pub_year_start: Optional[int] = None,
        scope: Optional[str] = None,
    ) -> list[PaperSearchItem]:
        self._init()

        # 필터를 고려해 후보를 넉넉하게 뽑음
        candidate_n = min(n_results * 4, self._collection.count())
        semantic_results = self._semantic_search(query, candidate_n)
        bm25_results = self._bm25_search(query, candidate_n)
        combined = _rrf_combine(semantic_results, bm25_results)

        items = []
        for doc_id, rrf_score in combined:
            if len(items) >= n_results:
                break
            paper = self._paper_index.get(doc_id)
            if not paper:
                continue
            # 발행연도 필터 (post-retrieval — Pubyear가 문자열로 저장됨)
            if pub_year_start:
                try:
                    if int(paper.get("Pubyear") or 0) < pub_year_start:
                        continue
                except (ValueError, TypeError):
                    continue
            # scope 필터 — DBCode 기준
            # KCI: JAKO / SCI계열: SCIE·SSCI·AHCI (현재 미적재, 추후 추가 가능)
            # ANY/None: 필터 없음
            if scope and scope not in ("ANY", "ALL"):
                db_code = paper.get("DBCode", "")
                if scope == "KCI" and db_code != "JAKO":
                    continue
                elif scope == "SCI" and db_code not in ("SCIE", "SSCI", "AHCI"):
                    continue
            items.append(_to_search_item(paper, rrf_score))
        return items

    async def search(
        self,
        query: str,
        n_results: int = 10,
        pub_year_start: Optional[int] = None,
        scope: Optional[str] = None,
    ) -> list[PaperSearchItem]:
        return await asyncio.to_thread(
            self._sync_search, query, n_results, pub_year_start, scope
        )

    def _sync_get_by_ids(self, ids: list[str]) -> list[PaperSearchItem]:
        """ID 목록으로 논문 직접 조회 — 유사도 계산 없음 (키워드 검색 전용)"""
        self._init()
        items = []
        for cn in ids:
            paper = self._paper_index.get(cn)
            if paper:
                items.append(_to_search_item(paper, 0.0))
        return items

    async def get_items_by_ids(self, ids: list[str]) -> list[PaperSearchItem]:
        return await asyncio.to_thread(self._sync_get_by_ids, ids)


_service: Optional[ChromaSearchService] = None


def get_chroma_search_service() -> ChromaSearchService:
    global _service
    if _service is None:
        _service = ChromaSearchService()
    return _service
