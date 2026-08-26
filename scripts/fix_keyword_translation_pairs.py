"""ko/en 번역쌍 사이의 잘못된 RELATED_TO 관계를 SAME_AS로 교정하는 1회성 마이그레이션.

배경: load_neo4j_graph.py가 예전엔 한 논문의 모든 ko/en 키워드 조합을 무조건 RELATED_TO로
연결해서, 같은 개념의 번역어(예: "미토콘드리아 유전체" ↔ "mitochondrial genome")끼리도
"관련 키워드"로 취급됐다. 그래서 키워드맵에서 앵커 자신의 번역어가 연관 키워드로 뜨는 문제가 있었다.

전체 그래프를 재구축(reset)하지 않고, 원본 JSON에서 번역쌍만 다시 계산해
1) SAME_AS 관계 추가 (추가만 하므로 안전)
2) 그 번역쌍 사이의 기존 RELATED_TO 관계만 삭제 (다른 노드/관계는 손대지 않음)
두 단계만 수행한다. Paper/Keyword/Author 등 다른 데이터는 전혀 건드리지 않는다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_neo4j_graph import DEFAULT_INPUT_PATH, _run_write_batch, build_graph_payload  # noqa: E402

from app.core.neo4j_client import get_neo4j_driver  # noqa: E402

_SAME_AS_QUERY = """
UNWIND $rows AS row
MATCH (a:Keyword {key: row.from_key})
MATCH (b:Keyword {key: row.to_key})
MERGE (a)-[r:SAME_AS]->(b)
SET r.loaded_at = row.loaded_at
"""

_DELETE_RELATED_TO_QUERY = """
UNWIND $rows AS row
MATCH (a:Keyword {key: row.from_key})-[r:RELATED_TO]-(b:Keyword {key: row.to_key})
DELETE r
"""

_COUNT_RELATED_TO = "MATCH ()-[r:RELATED_TO]->() RETURN count(r) AS cnt"
_COUNT_SAME_AS = "MATCH ()-[r:SAME_AS]->() RETURN count(r) AS cnt"


def main() -> None:
    print(f"입력 파일: {DEFAULT_INPUT_PATH}")
    data = json.loads(DEFAULT_INPUT_PATH.read_text(encoding="utf-8-sig"))
    payload = build_graph_payload(data)
    same_as_rows = payload.same_as
    print(f"번역쌍(SAME_AS 대상): {len(same_as_rows)}건")

    if not same_as_rows:
        print("번역쌍이 없어 종료합니다.")
        return

    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
            before_related = session.run(_COUNT_RELATED_TO).single()["cnt"]
            before_same_as = session.run(_COUNT_SAME_AS).single()["cnt"]
            print(f"[적용 전] RELATED_TO: {before_related}건, SAME_AS: {before_same_as}건")

            print("1) SAME_AS 관계 추가 중...")
            _run_write_batch(session, _SAME_AS_QUERY, same_as_rows, batch_size=500)

            print("2) 번역쌍 사이의 기존 RELATED_TO 관계 삭제 중...")
            _run_write_batch(session, _DELETE_RELATED_TO_QUERY, same_as_rows, batch_size=500)

            after_related = session.run(_COUNT_RELATED_TO).single()["cnt"]
            after_same_as = session.run(_COUNT_SAME_AS).single()["cnt"]
            print(f"[적용 후] RELATED_TO: {after_related}건 (삭제됨: {before_related - after_related}건), "
                  f"SAME_AS: {after_same_as}건 (추가됨: {after_same_as - before_same_as}건)")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
