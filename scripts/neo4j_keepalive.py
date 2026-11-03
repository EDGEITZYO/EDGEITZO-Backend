"""Neo4j Aura Free 자동 일시정지 방지용 하루 1회 접속.

Aura Free는 72시간 동안 활동이 없으면 일시정지되고, 일시정지가 30일을 넘기면 삭제된다.
읽기만으로 '활동'으로 쳐 주는지 확실하지 않아 전용 노드 하나에 시각을 덮어쓴다
(논문·인용 그래프 데이터와 라벨이 겹치지 않는다).

GitHub Actions(.github/workflows/neo4j-keepalive.yml)가 이틀에 한 번 실행한다.
접속 정보는 환경변수 NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD에서 읽는다.
실패하면 0이 아닌 코드로 끝나 워크플로가 실패로 표시된다(= GitHub 알림 메일).
"""
import os
import sys

from neo4j import GraphDatabase


def main() -> int:
    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USERNAME")
    password = os.environ.get("NEO4J_PASSWORD")
    if not (uri and user and password):
        print("NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD 환경변수가 필요합니다.", file=sys.stderr)
        return 1

    with GraphDatabase.driver(uri, auth=(user, password)) as driver:
        driver.verify_connectivity()
        records, _, _ = driver.execute_query(
            "MERGE (k:KeepAlive {id: 'aura-free'}) "
            "SET k.last_ping = datetime() "
            "RETURN toString(k.last_ping) AS last_ping"
        )
    print(f"Neo4j keepalive ok: {records[0]['last_ping']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
