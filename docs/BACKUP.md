# 저장소 백업 & 복구

클린 재구축(`--reset`) 전 반드시 실행한다. `--reset`은 비가역이다.

---

## 백업 실행

```bash
# 전체 3개 저장소 한 번에
python scripts/backup_stores.py

# 개별 선택
python scripts/backup_stores.py --stores postgres
python scripts/backup_stores.py --stores neo4j
python scripts/backup_stores.py --stores chroma

# 출력 경로 지정 (기본: data/backups/)
python scripts/backup_stores.py --out-dir /path/to/dir
```

백업 파일은 `data/backups/`에 타임스탬프 이름으로 저장된다 (`.gitignore` 제외).

| 저장소 | 파일명 패턴 | 방식 |
|--------|-----------|------|
| Postgres | `postgres_YYYYMMDD_HHMMSS.dump` | `pg_dump -Fc` (커스텀 포맷, 압축) |
| Neo4j | `neo4j_YYYYMMDD_HHMMSS.cypher` | APOC stream export (cypher-shell 포맷) |
| ChromaDB | `chroma_YYYYMMDD_HHMMSS.tar.gz` | Docker volume alpine tar |

---

## 저장소별 복구

### Postgres

```bash
# 기존 데이터를 덮어쓰며 복구 (--clean: 기존 객체 삭제 후 재생성)
PGPASSWORD=password pg_restore \
  -h localhost -U user -d edgeitzo \
  --clean --if-exists \
  data/backups/postgres_YYYYMMDD_HHMMSS.dump

# 또는 DB를 완전히 비운 뒤 복구 (더 안전)
PGPASSWORD=password psql -h localhost -U user -c "DROP DATABASE IF EXISTS edgeitzo;"
PGPASSWORD=password psql -h localhost -U user -c "CREATE DATABASE edgeitzo;"
PGPASSWORD=password pg_restore \
  -h localhost -U user -d edgeitzo \
  data/backups/postgres_YYYYMMDD_HHMMSS.dump
```

> **주의**: `bookmarks`, `recent_reads`는 `papers`에 CASCADE FK이므로, papers 복구 후 자동 복구됨.

---

### Neo4j

백업 파일(`.cypher`)은 cypher-shell 포맷이다. Python으로 재실행한다.

```bash
python - <<'EOF'
import os, sys
from pathlib import Path

BACKUP_FILE = "data/backups/neo4j_YYYYMMDD_HHMMSS.cypher"   # ← 파일명 교체

for line in Path(".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

from neo4j import GraphDatabase
driver = GraphDatabase.driver(
    os.environ["NEO4J_URI"],
    auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
)

cypher = Path(BACKUP_FILE).read_text(encoding="utf-8")

# cypher-shell 마커(:begin/:commit/:schema) 제거 후 세미콜론 단위로 분할
import re
stmts = [
    s.strip() for s in re.split(r";[\s]*\n", cypher)
    if s.strip() and not s.strip().startswith(":")
]

with driver.session() as session:
    for stmt in stmts:
        if stmt:
            session.run(stmt)

print(f"복구 완료: {len(stmts)}개 구문 실행")
driver.close()
EOF
```

> **주의**: 복구 전에 기존 그래프를 비워야 한다 (`MATCH (n) DETACH DELETE n`).  
> 현재 Aura 인스턴스에는 이전 프로젝트 노드가 혼재한다 — 클린 재구축 시 `load_neo4j_graph.py --reset`이 이를 처리한다.

---

### ChromaDB

> **주의**: 실제 데이터는 컨테이너 내부 `/data/` (chroma.sqlite3)에 있다.  
> `docker-compose.yml`의 volume mount(`/chroma/chroma`)가 실제 경로와 불일치해 **현재 데이터가 비영속 상태**다.  
> 컨테이너를 삭제하면 데이터가 사라진다. 클린 재구축 전 반드시 백업 실행.

```bash
# 1. ChromaDB 컨테이너 중지
docker stop papergraph-chromadb

# 2. 기존 /data 제거
docker run --rm \
  --volumes-from papergraph-chromadb \
  alpine sh -c "rm -rf /data/*"

# 3. 백업 파일 복원
docker run --rm \
  --volumes-from papergraph-chromadb \
  -v "$(pwd)/data/backups":/backup \
  alpine tar xzf /backup/chroma_YYYYMMDD_HHMMSS.tar.gz -C /

# 4. 컨테이너 재시작
docker start papergraph-chromadb
```

---

## 백업 전 체크리스트

```
[ ] Docker가 실행 중인가 (Postgres, ChromaDB 컨테이너)
[ ] Neo4j Aura 네트워크 접속 가능한가
[ ] data/backups/ 여유 공간 충분한가 (수백 MB 예상)
[ ] 백업 파일명(타임스탬프)을 복구 명령에 정확히 입력했는가
```

---

## 저장소 접속 정보

| 저장소 | 호스트 | 설정 위치 |
|--------|--------|---------|
| Postgres | `localhost:5432` DB `edgeitzo` | `.env` → `DATABASE_URL` |
| Neo4j | `neo4j+s://4294c862.databases.neo4j.io` | `.env` → `NEO4J_URI` |
| ChromaDB | `localhost:8001` | `.env` → `CHROMA_HOST/PORT`, Docker volume `chroma_data` |
