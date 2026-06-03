"""
검색 품질 평가
실행: python scripts/eval_search.py
지표: Recall@5, Recall@10, MRR, NDCG@10
relevant_paper_ids 비어있는 케이스는 N/A (구조 검증용)
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from app.services.search_service import execute_search

TEST_CASES = [
    {
        "query": "스트레스와 소비 충동성 연관성",
        "search_params": {
            "keywords": ["충동구매", "스트레스 대처", "감정 소비"],
            "scope": "ALL",
            "pub_year_start": None,
            "research_purpose": "연구주제탐색",
            "trust_level": None,
            "advanced_filters": {},
        },
        "relevant_paper_ids": [],  # 테스트에선 실제 paper_id 채울 것
    },
    {
        "query": "딥러닝 기반 의료 영상 진단",
        "search_params": {
            "keywords": ["딥러닝", "의료영상", "CNN"],
            "scope": "SCI",
            "pub_year_start": 2021,
            "research_purpose": "논문작성참고",
            "trust_level": None,
            "advanced_filters": {},
        },
        "relevant_paper_ids": [],
    },
    {
        "query": "한국어 자연어처리 트랜스포머",
        "search_params": {
            "keywords": ["트랜스포머", "한국어 NLP", "BERT"],
            "scope": "ALL",
            "pub_year_start": 2021,
            "research_purpose": "최신트렌드",
            "trust_level": None,
            "advanced_filters": {},
        },
        "relevant_paper_ids": [],
    },
    {
        "query": "그래프 신경망 추천시스템",
        "search_params": {
            "keywords": ["GNN", "추천시스템", "협업 필터링"],
            "scope": "ALL",
            "pub_year_start": None,
            "research_purpose": "연구주제탐색",
            "trust_level": None,
            "advanced_filters": {},
        },
        "relevant_paper_ids": [],
    },
    {
        "query": "강화학습 로봇 제어",
        "search_params": {
            "keywords": ["강화학습", "로봇 제어", "정책 학습"],
            "scope": "SCI",
            "pub_year_start": 2021,
            "research_purpose": "랩미팅발표",
            "trust_level": None,
            "advanced_filters": {},
        },
        "relevant_paper_ids": [],
    },
]


def recall_at_k(retrieved: list, relevant: list, k: int) -> float:
    if not relevant:
        return -1.0
    return len(set(retrieved[:k]) & set(relevant)) / len(relevant)


def mrr(retrieved: list, relevant: list) -> float:
    if not relevant:
        return -1.0
    for rank, pid in enumerate(retrieved, 1):
        if pid in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: list, relevant: list, k: int) -> float:
    if not relevant:
        return -1.0
    rel_set = set(relevant)
    dcg = sum(1 / math.log2(r + 1) for r, pid in enumerate(retrieved[:k], 1) if pid in rel_set)
    idcg = sum(1 / math.log2(r + 1) for r in range(1, min(len(relevant), k) + 1))
    return dcg / idcg if idcg else 0.0


async def run_eval():
    rows = []
    for case in TEST_CASES:
        print(f"평가 중: {case['query'][:30]}...", end=" ", flush=True)
        try:
            result = await execute_search(case["search_params"])
            ids = [p["paper_id"] for p in result["papers"]]
            rel = case["relevant_paper_ids"]
            rows.append((
                case["query"][:30],
                recall_at_k(ids, rel, 5),
                recall_at_k(ids, rel, 10),
                mrr(ids, rel),
                ndcg_at_k(ids, rel, 10),
                ids[:5],  # 상위 5개 paper_id 미리보기
            ))
            print("완료")
        except Exception as e:
            print(f"실패: {e}")
            rows.append((case["query"][:30], -1.0, -1.0, -1.0, -1.0, []))

    print(f"\n{'Query':<32} | {'R@5':>6} | {'R@10':>6} | {'MRR':>6} | {'NDCG@10':>7}")
    print("-" * 72)
    for q, r5, r10, m, n10, top_ids in rows:
        f = lambda v: f"{v:.2f}" if v >= 0 else "  N/A"
        print(f"{q:<32} | {f(r5):>6} | {f(r10):>6} | {f(m):>6} | {f(n10):>7}")
        if top_ids:
            print(f"  → 상위 paper_id: {top_ids}")

    valid = [(r5, r10, m, n10) for _, r5, r10, m, n10, _ in rows if r5 >= 0]
    if valid:
        avg = [sum(x) / len(x) for x in zip(*valid)]
        print(f"\n{'Average':<32} | {avg[0]:>6.2f} | {avg[1]:>6.2f} | {avg[2]:>6.2f} | {avg[3]:>7.2f}")
    print("\n※ N/A = relevant_paper_ids 미입력 (구조 검증용)")
    print("※ 실제 평가를 위해 TEST_CASES의 relevant_paper_ids에 paper_id를 채워주세요.")


if __name__ == "__main__":
    asyncio.run(run_eval())
