"""연구자 공저 그래프를 Neo4j에 적재하고 GNN(GraphSAGE)으로 그래프 임베딩을 만든다.

왜 GNN인가:
  내용 임베딩(embed_researchers.py)은 "무슨 단어를 쓰나"만 본다. 같은 분야라도 서로 다른
  연구 그룹이면 갈라야 하고, 표현이 달라도 같이 연구하면 붙어야 한다. 그 정보는
  공저 네트워크에 있다. GraphSAGE는 "이웃인 노드는 벡터도 가깝게" 학습하므로
  정답 라벨이 필요 없다(비지도).

그래프 규모 (실측):
  노드 2,674 / 고유 엣지 7,943 / 연결요소 367 / 최대 요소 453명 / 평균 차수 5.9
  외부 논문 공저를 넣기 전에는 최대 요소가 59명이라 GNN이 배울 이웃이 없었다.

사용법:
  python scripts/build_researcher_graph.py --load          # Neo4j에 노드/엣지 적재
  python scripts/build_researcher_graph.py --train         # GraphSAGE 학습 + 임베딩 저장
  python scripts/build_researcher_graph.py --load --train
  python scripts/build_researcher_graph.py --evaluate      # 내용 임베딩과 비교
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_ENV_PATH = PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        if _k.strip():
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import numpy as np
from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.core.neo4j_client import get_neo4j_driver

GRAPH_NAME = "researcher_coauthor"
NODE_LABEL = "ResearcherNode"   # 기존 :Author 노드(이름만 있는 옛 적재분)와 섞이지 않게 분리
REL_TYPE = "COAUTHORED"
FEATURE_PROPERTY = "feat"
# 1024차원을 그대로 넣으면 Aura에서 학습이 무거워진다. PCA로 줄여도 이웃 관계 학습에는 충분하다.
FEATURE_DIM = 128
EMBEDDING_DIM = 128

_EDGE_SQL = """
SELECT DISTINCT least(a.researcher_id, b.researcher_id) AS x,
                greatest(a.researcher_id, b.researcher_id) AS y
FROM researcher_papers a
JOIN researcher_papers b ON a.paper_id = b.paper_id AND a.researcher_id <> b.researcher_id
UNION
SELECT DISTINCT least(a.researcher_id, b.researcher_id),
                greatest(a.researcher_id, b.researcher_id)
FROM researcher_external_papers a
JOIN researcher_external_papers b
  ON a.external_id = b.external_id AND a.researcher_id <> b.researcher_id
"""


async def fetch_graph() -> tuple[list[dict], list[tuple[str, str]]]:
    async with AsyncSessionLocal() as session:
        nodes = (
            await session.execute(
                text(
                    "SELECT researcher_id, coalesce(author_name_kor, author_name_eng) AS name, "
                    "       embedding FROM researchers WHERE embedding IS NOT NULL"
                )
            )
        ).all()
        edges = (await session.execute(text(_EDGE_SQL))).all()
    return (
        [{"id": n.researcher_id, "name": n.name or "", "vec": n.embedding} for n in nodes],
        [(e.x, e.y) for e in edges],
    )


def reduce_features(nodes: list[dict]) -> np.ndarray:
    """내용 임베딩 1024차원 → PCA 128차원. GraphSAGE의 입력 피처로 쓴다."""
    matrix = np.array([n["vec"] for n in nodes], dtype=np.float32)
    centered = matrix - matrix.mean(axis=0, keepdims=True)
    # 표본(2,752) < 차원(1024)이 아니므로 공분산 대신 SVD로 바로 주성분을 얻는다.
    _u, _s, vt = np.linalg.svd(centered, full_matrices=False)
    reduced = centered @ vt[:FEATURE_DIM].T
    norms = np.linalg.norm(reduced, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (reduced / norms).astype(np.float32)


def load_to_neo4j(nodes: list[dict], features: np.ndarray, edges: list[tuple[str, str]]) -> None:
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            print(f"[neo4j] 기존 {NODE_LABEL} 정리")
            session.run(f"MATCH (n:{NODE_LABEL}) DETACH DELETE n")
            session.run(
                f"CREATE CONSTRAINT researcher_node_id IF NOT EXISTS "
                f"FOR (n:{NODE_LABEL}) REQUIRE n.researcher_id IS UNIQUE"
            )
            payload = [
                {"id": n["id"], "name": n["name"], "feat": [float(v) for v in f]}
                for n, f in zip(nodes, features)
            ]
            for start in range(0, len(payload), 500):
                session.run(
                    f"UNWIND $rows AS row "
                    f"MERGE (n:{NODE_LABEL} {{researcher_id: row.id}}) "
                    f"SET n.name = row.name, n.{FEATURE_PROPERTY} = row.feat",
                    rows=payload[start : start + 500],
                )
            print(f"[neo4j] 노드 {len(payload):,}개 적재")

            edge_rows = [{"x": x, "y": y} for x, y in edges]
            for start in range(0, len(edge_rows), 1000):
                session.run(
                    f"UNWIND $rows AS row "
                    f"MATCH (a:{NODE_LABEL} {{researcher_id: row.x}}), (b:{NODE_LABEL} {{researcher_id: row.y}}) "
                    f"MERGE (a)-[:{REL_TYPE}]-(b)",
                    rows=edge_rows[start : start + 1000],
                )
            count = session.run(
                f"MATCH (:{NODE_LABEL})-[r:{REL_TYPE}]-() RETURN count(r)/2 AS n"
            ).single()["n"]
            print(f"[neo4j] 엣지 {count:,}개 적재")
    finally:
        driver.close()


def train_graphsage(nodes: list[dict], features: np.ndarray, edges: list[tuple[str, str]]) -> dict[str, list[float]]:
    """비지도 GraphSAGE 학습.

    Neo4j GDS에도 GraphSAGE가 있지만 Aura Graph Analytics는 계산에 별도 유료 세션
    (gds.session.getOrCreate)이 필요하다. 비용 없이 재현 가능하도록 PyTorch로 직접 돌린다.
    Neo4j에 적재한 그래프는 조회·시각화용으로 그대로 둔다.
    """
    import torch

    from app.services.graphsage import TrainConfig, train_unsupervised

    index = {n["id"]: i for i, n in enumerate(nodes)}
    edge_array = np.array(
        [(index[x], index[y]) for x, y in edges if x in index and y in index], dtype=np.int64
    )
    device = "mps" if torch.backends.mps.is_available() else ("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[sage] 노드 {len(nodes):,} / 엣지 {len(edge_array):,} / device {device}")

    started = time.time()
    vectors = train_unsupervised(
        features, edge_array, TrainConfig(output_dim=EMBEDDING_DIM), device=device
    )
    print(f"[sage] 학습 완료 ({time.time()-started:.0f}초) 차원 {vectors.shape[1]}")
    return {n["id"]: vectors[i].tolist() for n, i in ((n, index[n["id"]]) for n in nodes)}


async def save_graph_embeddings(vectors: dict[str, list[float]]) -> None:
    async with AsyncSessionLocal() as session:
        for rid, vec in vectors.items():
            await session.execute(
                text(
                    "UPDATE researchers SET embedding_graph = :vec, updated_at = now() "
                    "WHERE researcher_id = :rid"
                ),
                {"vec": [float(v) for v in vec], "rid": rid},
            )
        await session.commit()
    print(f"[DB] 그래프 임베딩 {len(vectors):,}건 저장")


async def evaluate() -> None:
    """내용 임베딩과 그래프 임베딩이 무엇을 다르게 보는지 확인한다."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT researcher_id, coalesce(author_name_kor, author_name_eng) AS name, "
                    "       institution_current, embedding, embedding_graph "
                    "FROM researchers WHERE embedding IS NOT NULL AND embedding_graph IS NOT NULL"
                )
            )
        ).all()
    if not rows:
        print("[평가] 그래프 임베딩이 아직 없습니다.")
        return

    content = np.array([r.embedding for r in rows], dtype=np.float32)
    graph = np.array([r.embedding_graph for r in rows], dtype=np.float32)
    content /= np.linalg.norm(content, axis=1, keepdims=True)
    graph /= np.linalg.norm(graph, axis=1, keepdims=True)
    names = [r.name for r in rows]

    print(f"\n[평가] 대상 {len(rows):,}명")
    for target in ("인만진", "김수완"):
        matches = [i for i, n in enumerate(names) if n == target]
        if not matches:
            continue
        i = matches[0]
        print(f"\n  ■ {target} ({rows[i].institution_current})")
        for label, matrix in (("내용", content), ("그래프", graph)):
            order = np.argsort(-(matrix @ matrix[i]))[1:5]
            neighbours = ", ".join(f"{names[j]}({matrix[i] @ matrix[j]:.2f})" for j in order)
            print(f"     {label:4} 최근접: {neighbours}")


async def holdout_eval() -> None:
    """엣지의 10%를 빼고 학습해, 못 본 공저 관계를 맞히는지 본다.

    학습에 쓴 엣지로 재는 AUC는 외운 걸 되묻는 것이라 과대평가된다.
    임베딩이 그래프 구조를 '일반화'했는지는 held-out으로만 확인할 수 있다.
    """
    import torch

    from app.services.graphsage import TrainConfig, train_unsupervised

    nodes, edges = await fetch_graph()
    features = reduce_features(nodes)
    index = {n["id"]: i for i, n in enumerate(nodes)}
    all_edges = np.array([(index[x], index[y]) for x, y in edges if x in index and y in index])

    rng = np.random.default_rng(7)
    perm = rng.permutation(len(all_edges))
    cut = int(len(all_edges) * 0.9)
    train_edges, test_edges = all_edges[perm[:cut]], all_edges[perm[cut:]]
    print(f"[holdout] 학습 엣지 {len(train_edges):,} / 검증 엣지 {len(test_edges):,}")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    vectors = train_unsupervised(
        features, train_edges, TrainConfig(output_dim=EMBEDDING_DIM), device=device
    )
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    content = features / np.linalg.norm(features, axis=1, keepdims=True)

    existing = {tuple(sorted(e)) for e in all_edges.tolist()}
    negatives = []
    while len(negatives) < len(test_edges):
        a, b = rng.integers(0, len(nodes), size=2)
        if a != b and tuple(sorted((int(a), int(b)))) not in existing:
            negatives.append((int(a), int(b)))
    negatives = np.array(negatives)

    print("\n[holdout] 학습 때 못 본 공저 관계 예측 AUC")
    for label, matrix in (("내용 임베딩(PCA)", content), ("그래프 임베딩(GraphSAGE)", vectors)):
        pos = (matrix[test_edges[:, 0]] * matrix[test_edges[:, 1]]).sum(1)
        neg = (matrix[negatives[:, 0]] * matrix[negatives[:, 1]]).sum(1)
        auc = float((pos[:, None] > neg[None, :]).mean())
        print(f"   {label:26} AUC {auc:.3f}")


async def main_async(args) -> None:
    if args.holdout:
        await holdout_eval()
        return
    if args.evaluate:
        await evaluate()
        return

    nodes, edges = await fetch_graph()
    print(f"[준비] 노드 {len(nodes):,} / 엣지 {len(edges):,}")

    if args.load:
        features = reduce_features(nodes)
        print(f"[준비] 내용 임베딩 1024차원 → PCA {features.shape[1]}차원")
        load_to_neo4j(nodes, features, edges)

    if args.train:
        features = reduce_features(nodes)
        vectors = train_graphsage(nodes, features, edges)
        await save_graph_embeddings(vectors)
        await evaluate()


def main() -> None:
    parser = argparse.ArgumentParser(description="연구자 공저 그래프 적재 + GraphSAGE 임베딩")
    parser.add_argument("--load", action="store_true", help="Neo4j에 노드/엣지 적재")
    parser.add_argument("--train", action="store_true", help="GraphSAGE 학습 후 임베딩 저장")
    parser.add_argument("--evaluate", action="store_true", help="내용 임베딩과 비교만 수행")
    parser.add_argument("--holdout", action="store_true", help="엣지 10%를 빼고 학습해 일반화 성능 측정")
    args = parser.parse_args()
    if not (args.load or args.train or args.evaluate or args.holdout):
        parser.error("--load / --train / --evaluate / --holdout 중 하나는 지정해야 합니다.")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
