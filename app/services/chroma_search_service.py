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
import logging
import pickle
import re
from pathlib import Path
from typing import Optional

import chromadb
import numpy as np
from rank_bm25 import BM25Okapi

from app.core.settings import settings
from app.schemas.search import CredibilityInfo, PaperAuthor, PaperSearchItem
from app.services.embedding_model import get_bge_model

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# 코퍼스 단일 소스. 예전에는 scienceon_preprocessed.json을 1순위로 찾고 없으면 이 파일로
# 폴백했는데, 그 파일은 어느 환경에도 생성된 적이 없어 항상 폴백만 타고 있었다.
# 생성 스크립트(scripts/preprocess_corpus.py)는 Abstract를 형태소 어간 + TF-IDF 절삭 토큰
# 나열로 바꿔놓는데, BGE-m3는 자연어 문장으로 학습된 모델이라 그 텍스트로 임베딩하면
# 검색 품질이 떨어진다. 그래서 자동 탐색을 없애고 정규화 파일을 직접 가리킨다
# (전처리본을 쓰려면 embed_papers.py --input 으로 명시할 것).
_PAPERS_PATH = _PROJECT_ROOT / "data" / "parsed" / "scienceon_keywords_normalized.json"
# scripts/embed_abstract_sentences.py가 생성하는 문장 단위 임베딩 사전계산 캐시.
# 없으면 _batch_best_snippets가 그 논문에 한해서만 실시간 인코딩으로 폴백함.
_SENTENCE_CACHE_PATH = _PROJECT_ROOT / "data" / "parsed" / "abstract_sentence_embeddings.pkl"

_COLLECTION_NAME = "papers"
_RRF_K = 60  # RRF 상수 — 값이 클수록 하위 랭크 페널티 완화


def _build_where_clause(
    pub_year_start: Optional[int] = None,
    paper_type: Optional[str] = None,
    citation_min: Optional[int] = None,
    pub_year_exact: bool = False,
) -> Optional[dict]:
    """Chroma where절 조립 — 연도/논문유형/인용수 pre-filter.

    paper_type은 현재 $eq(단일값)만 지원. 다중 유형 배제 조건(예: 학위논문 제외)이 필요해지면 $in으로 확장 예정 — 지금은 미구현 (4단계 칩 생성 로직에서 실제 필요 여부 확인 후 확장).
    pub_year_exact=True면 pub_year_start를 "이상"이 아니라 정확히 그 해로 매칭 (논문 목록 연도 드롭다운 전용).
    """
    conditions = []
    if pub_year_start:
        if pub_year_exact:
            conditions.append({"Pubyear": {"$eq": pub_year_start}})
        else:
            conditions.append({"Pubyear": {"$gte": pub_year_start}})
    if paper_type:
        conditions.append({"DBCode": {"$eq": paper_type}})
    if citation_min is not None:
        conditions.append({"citation_count": {"$gte": citation_min}})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _load_papers() -> tuple[dict[str, dict], list[dict]]:
    data = json.loads(_PAPERS_PATH.read_text(encoding="utf-8"))
    papers: list[dict] = data.get("papers", data) if isinstance(data, dict) else data
    return {p["CN"]: p for p in papers if p.get("CN")}, papers


def _load_sentence_cache() -> dict[str, tuple[list[str], np.ndarray]]:
    """scripts/embed_abstract_sentences.py가 만든 사전계산 캐시 로드.
    없으면 빈 dict 반환 — _batch_best_snippets가 논문별로 실시간 인코딩으로 폴백함
    (캐시 미실행 상태에서도 서비스 자체는 정상 동작하도록, 다만 느림)."""
    if not _SENTENCE_CACHE_PATH.exists():
        logger.warning(
            "문장 임베딩 캐시(%s) 없음 — matched_snippet이 실시간 인코딩으로 느려질 수 있음. "
            "scripts/embed_abstract_sentences.py 실행 권장.",
            _SENTENCE_CACHE_PATH,
        )
        return {}
    with open(_SENTENCE_CACHE_PATH, "rb") as f:
        return pickle.load(f)


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


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom else 0.0


def _to_search_item(
    paper: dict,
    score: float,
    similarity_score: float = 0.0,
    matched_snippet: Optional[str] = None,
) -> PaperSearchItem:
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
        similarity_score=round(similarity_score, 4),
        matched_snippet=matched_snippet,
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
        self._sentence_cache: dict[str, tuple[list[str], np.ndarray]] = {}
        self._ready = False

    def _init(self) -> None:
        if self._ready:
            return

        self._paper_index, self._papers = _load_papers()

        tokenized = [_tokenize(_paper_to_bm25_text(p)) for p in self._papers]
        self._bm25 = BM25Okapi(tokenized)

        self._model = get_bge_model()
        self._sentence_cache = _load_sentence_cache()

        chroma_client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
        )
        self._collection = chroma_client.get_collection(_COLLECTION_NAME)
        self._ready = True

    def _encode_query(self, query: str) -> list[float]:
        # BGE-m3-ko 권장: 쿼리 임베딩 시 "query: " prefix
        return self._model.encode(f"query: {query}", convert_to_numpy=True).tolist()

    def _semantic_search(self, query_vec: list[float], n: int, where: Optional[dict] = None) -> list[tuple[str, float]]:
        results = self._collection.query(
            query_embeddings=[query_vec],
            n_results=min(n, self._collection.count()),
            where=where,
        )
        return [
            (doc_id, max(0.0, 1.0 - dist))
            for doc_id, dist in zip(results["ids"][0], results["distances"][0])
        ]

    def _batch_similarity_for_ids(self, ids: list[str], query_vec: list[float]) -> dict[str, float]:
        """semantic_results 후보에 없던 문서(BM25 단독 매칭)용 — 저장된 임베딩을 한 번에 조회해 코사인 계산."""
        if not ids:
            return {}
        data = self._collection.get(ids=ids, include=["embeddings"])
        query_arr = np.array(query_vec)
        return {
            doc_id: max(0.0, _cosine(np.array(emb), query_arr))
            for doc_id, emb in zip(data["ids"], data["embeddings"])
        }

    def _batch_best_snippets(
        self, abstracts: dict[str, Optional[str]], query_vec: list[float]
    ) -> dict[str, Optional[str]]:
        """doc_id→초록 맵을 받아 문서별로 검색어와 가장 유사한 문장 1개씩 반환.
        scripts/embed_abstract_sentences.py가 만든 사전계산 캐시(self._sentence_cache)에
        있으면 저장된 벡터로 코사인 계산만 한다(모델 인코딩 없음 — 후보 수와 무관하게 빠름).
        캐시에 없는 문서(스크립트 실행 후 새로 적재된 논문 등)만 그때그때 인코딩해서 폴백한다
        — 이런 문서가 대량이면 응답이 다시 느려지므로 캐시를 최신 상태로 유지해야 함."""
        doc_sentences: dict[str, list[str]] = {}
        query_arr = np.array(query_vec)
        best: dict[str, tuple[float, str]] = {}

        live_targets: dict[str, str] = {}
        for doc_id, abstract in abstracts.items():
            cached = self._sentence_cache.get(doc_id)
            if cached is not None:
                sentences, vecs = cached
                doc_sentences[doc_id] = sentences
                if len(vecs):
                    norms = np.linalg.norm(vecs, axis=1) * np.linalg.norm(query_arr)
                    sims = (vecs @ query_arr) / (norms + 1e-12)
                    idx = int(np.argmax(sims))
                    best[doc_id] = (float(sims[idx]), sentences[idx])
            else:
                sentences = _split_sentences(abstract) if abstract else []
                doc_sentences[doc_id] = sentences
                if abstract:
                    live_targets[doc_id] = abstract

        if live_targets:
            flat_sentences: list[str] = []
            owners: list[str] = []
            for doc_id in live_targets:
                sentences = doc_sentences[doc_id]
                flat_sentences.extend(sentences)
                owners.extend([doc_id] * len(sentences))
            if flat_sentences:
                sentence_vecs = self._model.encode(flat_sentences, convert_to_numpy=True)
                for doc_id, vec, sentence in zip(owners, sentence_vecs, flat_sentences):
                    sim = _cosine(vec, query_arr)
                    if doc_id not in best or sim > best[doc_id][0]:
                        best[doc_id] = (sim, sentence)

        return {
            doc_id: (sentences[0] if len(sentences) == 1 else best.get(doc_id, (0.0, None))[1])
            for doc_id, sentences in doc_sentences.items()
        }

    def _bm25_search(self, query: str, n: int) -> list[tuple[str, float]]:
        tokens = _tokenize(query)
        scores = self._bm25.get_scores(tokens)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:n]
        return [(self._papers[i]["CN"], float(scores[i])) for i in top_indices]

    def _sync_search(
        self,
        query: str,
        n_results: Optional[int] = None,
        pub_year_start: Optional[int] = None,
        scope: Optional[str] = None,
        paper_type: Optional[str] = None,
        citation_min: Optional[int] = None,
        pub_year_exact: bool = False,
    ) -> list[PaperSearchItem]:
        self._init()

        where_clause = _build_where_clause(pub_year_start, paper_type, citation_min, pub_year_exact)

        query_vec = self._encode_query(query)

        # RRF 랭킹(순수 점수 계산)은 코퍼스 전체를 대상으로 해도 저렴함(실측 약 3초, 대부분
        # 쿼리 인코딩 1회 비용) — where절 필터를 pub_year_start/paper_type처럼 정확하게 적용하기
        # 위해 candidate_n을 n_results*4로 미리 자르던 것을 없애고 전체를 후보로 삼는다.
        total = self._collection.count()
        semantic_results = self._semantic_search(query_vec, total, where=where_clause)
        bm25_results = self._bm25_search(query, total)
        semantic_score_map = dict(semantic_results)

        if where_clause:
            # BM25는 ChromaDB where절 대상이 아니므로, 동일 필터 기준 ID 집합을 별도 조회해 교집합만 남김
            filtered_ids = set(self._collection.get(where=where_clause, include=[])["ids"])
            bm25_results = [(doc_id, score) for doc_id, score in bm25_results if doc_id in filtered_ids]

        combined = _rrf_combine(semantic_results, bm25_results)

        # 관련도 하한선 — where절 통과 후보 중 1위 similarity_score 대비 search_relevance_ratio
        # 미만인 건 제외. 절대 점수 기준으로는 코퍼스 전체가 다 걸려버리는 경우가 있어
        # (질의와 진짜 무관한 문서도 완만하게 이어지는 분포라 절벽이 없음, 실측 확인됨)
        # "1위 대비 상대적으로 얼마나 안 맞는지"로 판단한다.
        top_similarity = semantic_results[0][1] if semantic_results else 0.0
        min_similarity = top_similarity * settings.search_relevance_ratio

        # 1차 패스: where절/scope/관련도 필터를 통과한 후보 확정 — similarity_score/snippet은
        # 후보 1건당 문장 단위 임베딩이 필요해 비용이 크므로(실측: 20건 9초, 150건 50초),
        # 이 비싼 2차 패스로 넘기기 전에 n_results로 반드시 자른다.
        selected: list[tuple[str, dict, float]] = []
        for doc_id, rrf_score in combined:
            if n_results is not None and len(selected) >= n_results:
                break
            paper = self._paper_index.get(doc_id)
            if not paper:
                continue
            if semantic_score_map.get(doc_id, 0.0) < min_similarity:
                continue
            # scope 필터 — DBCode 기준 (기존 로직 그대로 유지, 이번 작업 범위 아님)
            # KCI: JAKO / SCI계열: SCIE·SSCI·AHCI (현재 미적재, 추후 추가 가능)
            # ANY/None: 필터 없음
            if scope and scope not in ("ANY", "ALL"):
                db_code = paper.get("DBCode", "")
                if scope == "KCI" and db_code != "JAKO":
                    continue
                elif scope == "SCI" and db_code not in ("SCIE", "SSCI", "AHCI"):
                    continue
            selected.append((doc_id, paper, rrf_score))

        # 2차 패스: 확정된(=n_results로 이미 잘린) 후보에 대해서만 similarity_score/matched_snippet을
        # 배치로 한 번에 계산 (건마다 개별 encode()/get() 호출 시 결과 개수만큼 순차 호출이 생겨 응답 지연·타임아웃 유발됨)
        missing_ids = [doc_id for doc_id, _, _ in selected if doc_id not in semantic_score_map]
        fallback_scores = self._batch_similarity_for_ids(missing_ids, query_vec)
        abstracts = {
            doc_id: (paper.get("Abstract_original") or paper.get("Abstract"))
            for doc_id, paper, _ in selected
        }
        snippets = self._batch_best_snippets(abstracts, query_vec)

        items = []
        for doc_id, paper, rrf_score in selected:
            similarity_score = semantic_score_map.get(doc_id, fallback_scores.get(doc_id, 0.0))
            items.append(_to_search_item(
                paper, rrf_score, similarity_score=similarity_score, matched_snippet=snippets.get(doc_id)
            ))
        return items

    async def search(
        self,
        query: str,
        n_results: Optional[int] = None,
        pub_year_start: Optional[int] = None,
        scope: Optional[str] = None,
        paper_type: Optional[str] = None,
        citation_min: Optional[int] = None,
        pub_year_exact: bool = False,
    ) -> list[PaperSearchItem]:
        return await asyncio.to_thread(
            self._sync_search, query, n_results, pub_year_start, scope, paper_type, citation_min, pub_year_exact
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

    def _sync_get_citation_counts(self, ids: list[str]) -> dict[str, Optional[int]]:
        self._init()
        if not ids:
            return {}
        meta = self._collection.get(ids=ids, include=["metadatas"])
        return {
            doc_id: m.get("citation_count")
            for doc_id, m in zip(meta["ids"], meta["metadatas"])
        }

    async def get_citation_counts(self, ids: list[str]) -> dict[str, Optional[int]]:
        """ids에 대한 citation_count만 가볍게 조회 (필드 없으면 None). PostgreSQL 왕복 없음."""
        return await asyncio.to_thread(self._sync_get_citation_counts, ids)


_service: Optional[ChromaSearchService] = None


def get_chroma_search_service() -> ChromaSearchService:
    global _service
    if _service is None:
        _service = ChromaSearchService()
    return _service
