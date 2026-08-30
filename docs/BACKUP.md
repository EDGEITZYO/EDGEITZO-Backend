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

---

## S3 원격 보관 (권장)

### 왜 필요한가

2026-08-31 기준 EC2 상태를 확인한 결과 두 가지 문제가 있다.

1. **백업이 백업 대상과 같은 디스크에 있다.** `data/backups/`는 `/dev/root`(EBS) 위에 있고,
   백업 대상인 `postgres_data` 볼륨도 같은 EBS에 있다. 볼륨이 손상되면 원본과 백업이 함께 사라져
   백업의 의미가 없다.
2. **백업이 3개월 전 것이다.** EC2에 남은 덤프는 `postgres_20260602_005548.dump`(1.9MB)뿐인데,
   이후 적재된 researchers·papers 데이터가 들어 있지 않다.

용량은 문제가 아니다(41MB). 위치와 최신성이 문제다.

### 1) 버킷 생성 — 로컬에서 1회

리전은 EC2와 같은 `ap-northeast-2`(서울)로 맞춘다. 버킷 이름은 전역 고유여야 하므로 접미사를 붙인다.

```bash
BUCKET=edgeitzo-backups-$(date +%Y)   # 예: edgeitzo-backups-2026

aws s3api create-bucket \
  --bucket "$BUCKET" \
  --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2

# 퍼블릭 접근 전면 차단 (DB 덤프이므로 필수)
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# 실수로 덮어써도 이전 버전을 되살릴 수 있게 버저닝을 켠다
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

# 서버측 암호화
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

### 2) EC2에 권한 부여 — 액세스 키보다 IAM 역할

현재 EC2에는 IAM 역할이 붙어 있지 않다(`/latest/meta-data/iam/security-credentials/`가 비어 있음).
액세스 키를 서버에 파일로 두는 것보다 **인스턴스 역할**이 안전하다 — 키 파일 유출·만료 관리가 없어진다.

콘솔에서: IAM → 역할 → 역할 만들기 → AWS 서비스 → EC2 → 아래 정책을 인라인으로 붙이고,
EC2 → 인스턴스 → 작업 → 보안 → IAM 역할 수정에서 부착한다. 재부팅은 필요 없다.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::edgeitzo-backups-2026",
      "arn:aws:s3:::edgeitzo-backups-2026/*"
    ]
  }]
}
```

`s3:DeleteObject`를 일부러 뺐다. 서버가 침해돼도 원격 백업은 지우지 못하게 하기 위함이다.
정리는 아래 라이프사이클 규칙이 대신 한다.

`s3:CreateBucket`도 없다 — 버킷은 1단계에서 콘솔로 미리 만들어야 하고, EC2에서는 만들 수 없다.
버킷이 없는 상태로 업로드하면 `NoSuchBucket`이 난다.

### 3) EC2에 aws CLI 설치 (미설치 상태)

```bash
ssh edgeitzo
sudo apt-get update && sudo apt-get install -y unzip
curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp && sudo /tmp/aws/install
aws sts get-caller-identity     # 역할이 잘 붙었으면 Arn에 assumed-role/... 이 보인다
```

### 4) 최신 백업 생성 후 업로드

기존 6월 덤프를 그대로 올리면 안 된다. 먼저 지금 상태를 뜬다.

EC2의 Postgres 계정은 `edgeitzo`다(로컬 개발 환경의 `user`와 다르므로 주의).

EC2의 Postgres 계정은 `edgeitzo`다(로컬 개발 환경의 `user`와 다르므로 주의).
경로는 절대경로로 쓴다 — 재접속하면 `cd`가 풀려서 홈에서 실행되고 "path does not exist"가 난다.

```bash
BACKUP_DIR=~/EDGEITZO-Backend/data/backups
BUCKET=edgeitzo-backups-2026

docker exec edgeitzo-postgres pg_dump -U edgeitzo -Fc edgeitzo \
  > "$BACKUP_DIR/postgres_$(date +%Y%m%d_%H%M%S).dump"

# 0바이트가 아닌지 반드시 확인 — 리다이렉션은 실패해도 파일을 만든다
ls -lh "$BACKUP_DIR"

aws s3 sync "$BACKUP_DIR/" "s3://$BUCKET/ec2/" --storage-class STANDARD_IA
```

### 5) 업로드 검증 — 지우기 전에 반드시

크기가 바이트 단위까지 같은지 대조한다. 이게 맞아야 로컬 사본을 지울 수 있다.

```bash
echo "--- 로컬 ---"; ls -l "$BACKUP_DIR" | awk 'NR>1{print $5, $9}' | sort -k2
echo "--- S3  ---"; aws s3 ls "s3://$BUCKET/ec2/" | awk '{print $3, $4}' | sort -k2
```

### 6) 라이프사이클 — 오래된 것은 자동으로 싸게

```bash
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "archive-old-backups",
      "Status": "Enabled",
      "Filter": {"Prefix": "ec2/"},
      "Transitions": [{"Days": 30, "StorageClass": "GLACIER_IR"}],
      "NoncurrentVersionExpiration": {"NoncurrentDays": 180}
    }]
  }'
```

41MB 기준 월 비용은 STANDARD_IA로 약 $0.0005, Glacier IR 전환 후엔 그 절반 수준이다.
사실상 무료이므로 보관 주기를 아끼려 애쓸 필요가 없다.

### 7) EC2 디스크 정리 — 5번 검증을 통과한 뒤에만

```bash
# 최신 2개만 남기고 나머지는 S3에 있으므로 삭제
cd ~/EDGEITZO-Backend/data/backups && ls -t | tail -n +3 | xargs -r rm -v
```

### 정기 실행

주 1회 cron으로 돌리면 최신성 문제가 재발하지 않는다.

```bash
# crontab -e  (매주 일요일 04:00 KST)
0 4 * * 0 cd ~/EDGEITZO-Backend && docker exec edgeitzo-postgres pg_dump -U edgeitzo -Fc edgeitzo > data/backups/postgres_$(date +\%Y\%m\%d_\%H\%M\%S).dump && /usr/local/bin/aws s3 sync data/backups/ s3://edgeitzo-backups-2026/ec2/ --storage-class STANDARD_IA && ls -t data/backups | tail -n +3 | xargs -r -I{} rm data/backups/{}
```
