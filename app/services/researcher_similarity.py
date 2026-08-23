"""연구자 유사도 — 「노드 그래프 뷰」와 유사 연구자 추천에 쓰는 점수.

이 모듈은 **점수만** 낸다. 노드를 몇 px로 그릴지, 선을 그을지 말지, 어떻게 배치할지는
프런트가 정한다.

명세서의 「노드 그래프 뷰」는 중심에 검색한 키워드가 있고 연구자들이 그 주위를 둘러싸는
형태다(로우와프 3쪽). 명세서 문구도 "**키워드에** 가까울수록 연구 분야 유사"다 —
연구자끼리의 거리가 아니라 **연구자와 키워드 사이의 거리**를 재야 한다.

두 종류의 벡터를 쓴다:
  embedding        내용 기반(BGE-m3-ko). "무슨 주제를 연구하나"
  embedding_graph  공저 네트워크 기반(GraphSAGE). "누구와 함께 연구하나"

키워드와의 거리는 내용 임베딩만 쓴다 — 키워드는 그래프에 노드가 없으므로 비교 대상이 없다.
연구자끼리의 유사도(유사 연구자 추천)에서만 두 벡터를 섞는다.
"""
from __future__ import annotations

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 유사 연구자 추천에서 내용 임베딩에 두는 무게.
# 그래프 임베딩만 쓰면 같은 연구실 사람이 유사도 1.0에 붙어, 연구 분야가 아니라
# 소속 그룹으로 뭉친 결과가 나온다(공저 관계를 학습한 목적함수라 당연한 결과다).
DEFAULT_CONTENT_WEIGHT = 0.7


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


async def load_profiles(db: AsyncSession, researcher_ids: list[str]) -> list[dict]:
    """연구자 목록의 표시용 정보 + 벡터. 벡터가 없는 연구자는 빠진다."""
    if not researcher_ids:
        return []
    rows = (
        await db.execute(
            text(
                "SELECT researcher_id, coalesce(author_name_kor, author_name_eng) AS name, "
                "       institution_current, institution_dept, "
                "       coalesce(total_citations, 0) AS citations, citation_source, "
                "       total_papers, embedding, embedding_graph "
                "FROM researchers WHERE researcher_id = ANY(:ids) AND embedding IS NOT NULL"
            ),
            {"ids": researcher_ids},
        )
    ).all()
    return [
        {
            "researcher_id": r.researcher_id,
            "name": r.name,
            "institution": r.institution_current,
            "department": r.institution_dept,
            "citations": int(r.citations or 0),
            # KCI(국내 등재지)와 OpenAlex(국제)는 집계 범위가 달라 중앙값이 35배 차이난다.
            # 화면에서 라벨을 나눠 붙이고, 크기를 매길 때도 출처별로 따로 정규화해야 한다.
            "citation_source": r.citation_source,
            "total_papers": r.total_papers,
            "_content": r.embedding,
            "_graph": r.embedding_graph,
        }
        for r in rows
    ]


def _encode_keyword(keyword: str) -> np.ndarray:
    from app.services.embedding_model import get_bge_model

    # BGE-m3-ko 권장: 쿼리 임베딩 시 "query: " prefix (chroma_search_service와 동일)
    vector = get_bge_model().encode(f"query: {keyword}", convert_to_numpy=True)
    return vector / (np.linalg.norm(vector) or 1.0)


async def field_affinity(
    db: AsyncSession, keyword: str, researcher_ids: list[str]
) -> list[dict]:
    """분야 검색 결과의 각 연구자가 그 키워드와 얼마나 가까운지.

    「노드 그래프 뷰」에서 중심(키워드)과 각 연구자 노드 사이의 거리로 쓴다.
    반환하는 건 0~1 점수뿐이고, 그걸 몇 px 떨어뜨릴지는 프런트가 정한다.

    명세서 원안인 '키워드 공유 개수'는 분야 검색 결과 안에서 쌍의 45%가 0으로 나와
    그래프 절반이 그려지지 않는다(실측). 의미 유사도를 쓰면 표기가 달라도 잡힌다 —
    '효소 분해'와 'Alcalase-enzymatic hydrolysate'가 그런 경우다.
    """
    profiles = await load_profiles(db, researcher_ids)
    if not profiles:
        return []

    query_vector = _encode_keyword(keyword)
    content = _normalize(np.array([p["_content"] for p in profiles], dtype=np.float32))
    scores = content @ query_vector

    result = []
    for profile, score in zip(profiles, scores):
        item = {k: v for k, v in profile.items() if not k.startswith("_")}
        item["affinity"] = round(float(score), 4)
        result.append(item)
    result.sort(key=lambda x: -x["affinity"])
    return result


async def similar_researchers(
    db: AsyncSession,
    researcher_id: str,
    *,
    limit: int = 10,
    content_weight: float = DEFAULT_CONTENT_WEIGHT,
) -> list[dict]:
    """비슷한 연구를 하는 사람. 명세서엔 없지만 벡터가 있으면 따라 나온다.

    내용 임베딩과 그래프 임베딩의 유사도 상관계수는 0.227로 서로 다른 것을 본다 —
    내용은 '같은 주제', 그래프는 '같은 그룹'이다. 섞어서 둘 다 반영한다.
    """
    rows = (
        await db.execute(
            text(
                "SELECT researcher_id, coalesce(author_name_kor, author_name_eng) AS name, "
                "       institution_current, coalesce(total_citations, 0) AS citations, "
                "       citation_source, embedding, embedding_graph "
                "FROM researchers WHERE embedding IS NOT NULL"
            )
        )
    ).all()
    index = {r.researcher_id: i for i, r in enumerate(rows)}
    if researcher_id not in index:
        return []
    target = index[researcher_id]

    content = _normalize(np.array([r.embedding for r in rows], dtype=np.float32))
    scores = content @ content[target]
    if all(r.embedding_graph for r in rows):
        graph = _normalize(np.array([r.embedding_graph for r in rows], dtype=np.float32))
        scores = content_weight * scores + (1 - content_weight) * (graph @ graph[target])

    out = []
    for i in np.argsort(-scores):
        if rows[i].researcher_id == researcher_id:
            continue
        out.append(
            {
                "researcher_id": rows[i].researcher_id,
                "name": rows[i].name,
                "institution": rows[i].institution_current,
                "citations": int(rows[i].citations or 0),
                "citation_source": rows[i].citation_source,
                "similarity": round(float(scores[i]), 4),
            }
        )
        if len(out) >= limit:
            break
    return out


async def pairwise_similarity(
    db: AsyncSession, researcher_ids: list[str], *, content_weight: float = DEFAULT_CONTENT_WEIGHT
) -> tuple[list[dict], np.ndarray]:
    """연구자 목록의 쌍별 유사도 행렬.

    「노드 그래프 뷰」는 중심-연구자 거리만 쓰므로 여기선 필요 없다.
    연구자끼리의 관계가 필요한 다른 화면을 위해 남겨둔다.
    """
    profiles = await load_profiles(db, researcher_ids)
    if not profiles:
        return [], np.empty((0, 0))

    content = _normalize(np.array([p["_content"] for p in profiles], dtype=np.float32))
    matrix = content @ content.T
    if all(p["_graph"] for p in profiles):
        graph = _normalize(np.array([p["_graph"] for p in profiles], dtype=np.float32))
        matrix = content_weight * matrix + (1 - content_weight) * (graph @ graph.T)
    np.fill_diagonal(matrix, 1.0)

    cleaned = [{k: v for k, v in p.items() if not k.startswith("_")} for p in profiles]
    return cleaned, matrix
