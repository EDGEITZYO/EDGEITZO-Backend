"""임베딩 유사도로 ko/en 번역쌍을 추가 검출해 SAME_AS로 교정하는 2차 마이그레이션.

배경: load_neo4j_graph.py의 인덱스 기반 번역쌍 검출(Keyword[i]<->Keyword2[i])은
원본 데이터의 두 리스트 길이가 다르거나 순서가 안 맞는 논문에서는 동작하지 않는다.
이 스크립트는 이미 만들어둔 키워드 임베딩(Chroma 'keywords' 컬렉션)의 의미 유사도로
그런 잔여 케이스를 추가로 잡아낸다.

실측 기준(scripts/fix_keyword_translation_pairs.py 실행 후 확인):
- 진짜 번역쌍: 0.80~0.88 (미토콘드리아 유전체<->mitochondrial genome: 0.796, 관리<->management: 0.884)
- 무관한 공출현: 0.48~0.54
임계값 0.7로 충분히 안전하게 구분됨.

RELATED_TO(en-ko)만 대상으로 하고, 다른 노드/관계는 건드리지 않는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import chromadb  # noqa: E402
from load_neo4j_graph import _run_write_batch  # noqa: E402

from app.core.neo4j_client import get_neo4j_driver  # noqa: E402
from app.core.settings import settings  # noqa: E402

_SIMILARITY_THRESHOLD = 0.7
_COLLECTION_NAME = "keywords"

_FETCH_EN_KO_RELATED_TO = """
MATCH (a:Keyword)-[r:RELATED_TO]->(b:Keyword)
WHERE r.lang_pair = 'en-ko'
RETURN a.key AS from_key, b.key AS to_key
"""

_SAME_AS_QUERY = """
UNWIND $rows AS row
MATCH (a:Keyword {key: row.from_key})
MATCH (b:Keyword {key: row.to_key})
MERGE (a)-[r:SAME_AS]->(b)
SET r.loaded_at = row.loaded_at, r.detected_by = 'embedding'
"""

_DELETE_RELATED_TO_QUERY = """
UNWIND $rows AS row
MATCH (a:Keyword {key: row.from_key})-[r:RELATED_TO]-(b:Keyword {key: row.to_key})
DELETE r
"""


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom else 0.0


def main() -> None:
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            edges = session.run(_FETCH_EN_KO_RELATED_TO).data()
        print(f"검사 대상 en-ko RELATED_TO: {len(edges)}건")

        keys = sorted({e["from_key"] for e in edges} | {e["to_key"] for e in edges})
        client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        collection = client.get_collection(_COLLECTION_NAME)

        embeddings: dict[str, np.ndarray] = {}
        batch_size = 500
        for i in range(0, len(keys), batch_size):
            batch_keys = keys[i : i + batch_size]
            data = collection.get(ids=batch_keys, include=["embeddings"])
            for key, emb in zip(data["ids"], data["embeddings"]):
                embeddings[key] = np.array(emb)
        print(f"임베딩 조회: {len(embeddings)}/{len(keys)}건")

        from datetime import datetime

        loaded_at = datetime.now().isoformat(timespec="seconds")
        to_convert: list[dict[str, str]] = []
        for e in edges:
            from_key, to_key = e["from_key"], e["to_key"]
            if from_key not in embeddings or to_key not in embeddings:
                continue
            sim = _cosine(embeddings[from_key], embeddings[to_key])
            if sim >= _SIMILARITY_THRESHOLD:
                to_convert.append({"from_key": from_key, "to_key": to_key, "loaded_at": loaded_at})

        print(f"번역쌍으로 판정(유사도 >= {_SIMILARITY_THRESHOLD}): {len(to_convert)}건")

        if not to_convert:
            print("추가로 교정할 게 없습니다.")
            return

        with driver.session() as session:
            print("SAME_AS 추가 중...")
            _run_write_batch(session, _SAME_AS_QUERY, to_convert, batch_size=500)
            print("해당 RELATED_TO 삭제 중...")
            _run_write_batch(session, _DELETE_RELATED_TO_QUERY, to_convert, batch_size=500)

        print("완료.")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
