# Session Notes — feat/erd-infra

---

## 세션 1/3 — SQLAlchemy 모델 + Alembic 마이그레이션

### 생성된 모델 파일
| 파일 | 클래스 | 테이블 |
|------|--------|--------|
| `app/models/journal.py` | `Journal` | `journals` |
| `app/models/paper.py` | `Paper` | `papers` |
| `app/models/bookmark.py` | `BookmarkFolder`, `Bookmark` | `bookmark_folders`, `bookmarks` |
| `app/models/recent_read.py` | `RecentRead` | `recent_reads` |

### 생성된 마이그레이션
| 파일 | 내용 | down_revision |
|------|------|---------------|
| `002_create_journals_table.py` | journals + GIN(issn) + btree(sjr_sourceid, title) | 001 |
| `003_create_papers_table.py` | papers + 6개 인덱스 + FK→journals(SET NULL) | 002 |
| `004_create_bookmark_folders_table.py` | bookmark_folders + FK→users(CASCADE) | 003 |
| `005_create_bookmarks_table.py` | bookmarks + UniqueConstraint(user_id,paper_id) + FK 3개 | 004 |
| `006_create_recent_reads_table.py` | recent_reads + 복합인덱스 2개 + FK 2개 | 005 |

### 수정된 파일
- `app/models/__init__.py` — 모든 모델 import 추가
- `alembic/env.py` — `import app.models` 추가

### 주의사항
- autogenerate 시 users 테이블 diff 발생 (JSONB↔JSON 노이즈) → 무시
- `.venv` (Python 3.9) 사용 불가 → `.venv1` (Python 3.11) 사용
- alembic 실행: `.venv1/bin/alembic upgrade head`
- Docker 소켓: `DOCKER_HOST=unix:///Users/yuri/.docker/run/docker.sock`

---

## 세션 2/3 — SJR CSV 적재

### 적재 결과
- **적재된 journals 건수: 4,617건** (dedup 후, 2023~2025 agri+biochem 6개 파일)
- 스크립트: `scripts/load_sjr_journals.py`

### sjr_year 분포 (파일 기준)
| 연도 | 파일 | 행수 |
|------|------|------|
| 2023 | agri + biochem | 2,693 + 2,315 = 5,008 |
| 2024 | agri + biochem | 2,676 + 2,279 = 4,955 |
| 2025 | agri + biochem | 2,654 + 2,256 = 4,910 |
| — | dedup 후 최신연도 우선 | **4,617** |

### Quartile 분포
| 등급 | 건수 |
|------|------|
| Q1 | 1,243 |
| Q2 | 1,158 |
| Q3 | 1,087 |
| Q4 | 1,063 |
| - (미분류) | 66 |

### 스크립트 사용법
```bash
# 파싱만 (DB 미적재)
.venv1/bin/python scripts/load_sjr_journals.py --dry-run

# 초기화 후 적재
DOCKER_HOST=unix:///Users/yuri/.docker/run/docker.sock \
  .venv1/bin/python scripts/load_sjr_journals.py --reset

# 추가 파일 적재 (upsert — 안전하게 재실행 가능)
DOCKER_HOST=unix:///Users/yuri/.docker/run/docker.sock \
  .venv1/bin/python scripts/load_sjr_journals.py
```

### 트러블슈팅 기록
| 문제 | 원인 | 해결 |
|------|------|------|
| `StringDataRightTruncationError` | `coverage` 컬럼 11건이 100자 초과 | `clean_dataframe`에서 truncate 적용 |
| 중복 컬럼명 `Publisher` | SJR CSV에 Publisher가 2번 등장 | `df.loc[:, ~df.columns.duplicated()]` |
| asyncio "different loop" | `asyncio.run()` 3번 호출로 loop 충돌 | 단일 `async def _run()` 안에서 순차 실행 |
| SJR 소수점 "11,748" 파싱 실패 | `dtype=str`로 읽어서 `decimal=','` 무효 | `.str.replace(",", ".")` 후 `pd.to_numeric` |

### 다음 세션(세션 3)을 위한 인터페이스
세션 3이 Celery 작업이라면 journals 테이블에서 조회할 주요 인터페이스:
```python
# sjr_sourceid로 저널 조회
await session.execute(select(Journal).where(Journal.sjr_sourceid == sid))

# ISSN으로 저널 조회 (GIN 인덱스 활용)
from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import ARRAY
await session.execute(
    select(Journal).where(Journal.issn.any(issn_value))
)

# sci_indexed 저널만 조회
await session.execute(select(Journal).where(Journal.sci_indexed == True))
```

---

## 세션 3/3 — Celery 인스턴스 + health 태스크

### 생성된 파일
| 파일 | 역할 |
|------|------|
| `app/celery_app.py` | Celery 인스턴스 (broker=Redis DB1, backend=Redis DB2) |
| `app/tasks/__init__.py` | 태스크 패키지 초기화 |
| `app/tasks/health.py` | ping / echo / add 검증용 태스크 3종 |
| `scripts/test_celery.py` | 로컬에서 태스크 동작 검증 스크립트 |

### 새 태스크 추가 방법
1. `app/tasks/` 아래 새 모듈 생성 (예: `app/tasks/embedding.py`)
2. `app/celery_app.py`의 `include` 리스트에 등록:
   ```python
   include=[
       "app.tasks.health",
       "app.tasks.embedding",  # 추가
   ]
   ```
3. Docker 워커 재빌드: `docker compose up -d --build celery_worker`

### Redis DB 번호 컨벤션
| DB | 용도 |
|----|------|
| 0 | auth 캐시 (예약) |
| 1 | Celery broker |
| 2 | Celery result backend |
| 3+ | 향후 다른 용도 |

### 현재 등록된 태스크
| task name | 설명 |
|-----------|------|
| `health.ping` | 단순 pong 반환 |
| `health.echo` | 입력 메시지 그대로 반환 |
| `health.add` | x + y 계산, task_id 포함 반환 |

### 트러블슈팅 기록 — Redis 이중화 문제
| 문제 | 원인 | 해결 |
|------|------|------|
| 태스크 timeout | 로컬 Homebrew Redis(localhost:6379, 비밀번호 있음)와 Docker Redis(redis:6379, 비밀번호 없음)가 공존 → 테스트 스크립트와 워커가 서로 다른 Redis를 바라봄 | docker-compose celery_worker에 `CELERY_BROKER_URL: redis://:120809@host.docker.internal:6379/1` 설정 (워커가 호스트 Redis 사용) |
| AuthenticationError | 로컬 Redis에 비밀번호 필요 | `.env`에 `CELERY_BROKER_URL=redis://:120809@localhost:6379/1` 추가 |

### 동작 검증 명령
```bash
# 워커 로그 확인
docker logs papergraph-celery-worker --tail=20

# 태스크 테스트
DOCKER_HOST=unix:///Users/yuri/.docker/run/docker.sock \
  .venv1/bin/python scripts/test_celery.py
```

---

## 공통 주의사항

- **venv**: `.venv1` (Python 3.11) 사용
- **Docker 소켓**: `export DOCKER_HOST=unix:///Users/yuri/.docker/run/docker.sock` (~/.zshrc에 등록됨)
- **DB 확인**: `docker exec papergraph-postgres psql -U user -d edgeitzo -c "\dt"`
