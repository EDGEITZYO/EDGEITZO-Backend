"""
similarity_score(코사인 유사도) 분포 확인 — UI에 %로 보여줄 때 쓸 바닥/천장값 보정용
실행: python scripts/calibrate_similarity_score.py
(chroma/redis 등 실서비스 스택이 떠있는 환경에서 실행해야 함 — EC2 또는 docker-compose up 상태의 로컬)
"""
from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from app.services.chroma_search_service import get_chroma_search_service

QUERIES = [
    "스트레스와 소비 충동성 연관성",
    "딥러닝 기반 의료 영상 진단",
    "한국어 자연어처리 트랜스포머",
    "그래프 신경망 추천시스템",
    "강화학습 로봇 제어",
]


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(len(values) * p))
    return values[idx]


async def main() -> None:
    service = get_chroma_search_service()
    all_scores: list[float] = []

    for query in QUERIES:
        items = await service.search(query=query, n_results=20)
        scores = [it.similarity_score for it in items]
        all_scores.extend(scores)
        if scores:
            print(f"[{query}] n={len(scores)} max={max(scores):.4f} median={statistics.median(scores):.4f} min={min(scores):.4f}")
        else:
            print(f"[{query}] 결과 없음")

    print("\n=== 전체 분포 ===")
    if all_scores:
        print(f"n={len(all_scores)}")
        print(f"min={min(all_scores):.4f}")
        print(f"p10={_percentile(all_scores, 0.10):.4f}")
        print(f"p25={_percentile(all_scores, 0.25):.4f}")
        print(f"p50(median)={_percentile(all_scores, 0.50):.4f}")
        print(f"p75={_percentile(all_scores, 0.75):.4f}")
        print(f"p90={_percentile(all_scores, 0.90):.4f}")
        print(f"max={max(all_scores):.4f}")
    else:
        print("점수 데이터 없음")


if __name__ == "__main__":
    asyncio.run(main())
