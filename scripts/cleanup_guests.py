"""데모 기간 게스트 계정(is_guest=true)과 연관 데이터를 일괄 삭제한다.

지우는 것:
  PostgreSQL : bookmarks, bookmark_folders, recent_reads, user_keyword_maps, users
               (FK가 ON DELETE CASCADE지만 탈퇴 로직과 같이 자식부터 명시적으로 지운다)
  Redis DB 7 : recent_searches:{user_id}(홈 최근 탐색 이력), researcher_searches:{user_id}
  Redis rate limit DB : rl:llm:guest:{user_id}
  Neo4j      : 유저 데이터를 두지 않으므로 대상 없음

지우지 않는 것:
  - 토큰 블랙리스트(TTL로 사라짐), IP 기준 rate limit 카운터(TTL로 사라짐)
  - 검색 세션 상태(search 세션 키는 session_id 기준이고 TTL 1시간)
  이미 발급된 게스트 access 토큰은 계정이 지워지면 인증 API에서 401이 된다.

사용법:
  python scripts/cleanup_guests.py --dry-run                     # 삭제 대상 건수만 확인
  python scripts/cleanup_guests.py --older-than-days 7 --dry-run # 7일보다 오래된 게스트만
  python scripts/cleanup_guests.py                               # 전체 게스트 삭제
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
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

from sqlalchemy import delete, func, select

from app.core.database import AsyncSessionLocal
from app.core.rate_limit import llm_guest_key
from app.core.redis import get_redis
from app.core.settings import settings
from app.models.bookmark import Bookmark, BookmarkFolder
from app.models.recent_read import RecentRead
from app.models.user import User
from app.models.user_keyword_map import UserKeywordMap

# home.py·researcher_search_service.py가 유저별 이력을 두는 DB/키
_HISTORY_REDIS_DB = 7
_HISTORY_KEYS = ("recent_searches:{user_id}", "researcher_searches:{user_id}")

# 자식 → 부모 순서 (bookmarks.folder_id가 bookmark_folders를 참조)
_CHILD_TABLES = (
    ("bookmarks", Bookmark),
    ("bookmark_folders", BookmarkFolder),
    ("recent_reads", RecentRead),
    ("user_keyword_maps", UserKeywordMap),
)


def _redis_targets(user_ids: list[str]) -> list[tuple[int, str]]:
    targets = [
        (_HISTORY_REDIS_DB, tpl.format(user_id=uid)) for uid in user_ids for tpl in _HISTORY_KEYS
    ]
    targets += [(settings.redis_rate_limit_db, llm_guest_key(uid)) for uid in user_ids]
    return targets


def _existing_redis_keys(targets: list[tuple[int, str]]) -> list[tuple[int, str]]:
    found = []
    for db in sorted({db for db, _ in targets}):
        keys = [k for d, k in targets if d == db]
        r = get_redis(db)
        pipe = r.pipeline(transaction=False)
        for k in keys:
            pipe.exists(k)
        found += [(db, k) for k, hit in zip(keys, pipe.execute()) if hit]
    return found


async def run(dry_run: bool, older_than_days: int) -> None:
    async with AsyncSessionLocal() as db:
        cond = [User.is_guest.is_(True)]
        if older_than_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            cond.append(User.created_at < cutoff)

        ids = list((await db.execute(select(User.id).where(*cond))).scalars())
        print(f"대상 게스트: {len(ids)}명" + (f" (생성 {older_than_days}일 초과)" if older_than_days > 0 else ""))
        if not ids:
            return

        print("[PostgreSQL]")
        for name, model in _CHILD_TABLES:
            n = (await db.execute(select(func.count()).select_from(model).where(model.user_id.in_(ids)))).scalar_one()
            print(f"  {name:<18} {n}")
        print(f"  {'users':<18} {len(ids)}")

        str_ids = [str(i) for i in ids]
        try:
            redis_keys = _existing_redis_keys(_redis_targets(str_ids))
            print(f"[Redis] 키 {len(redis_keys)}개")
        except Exception as e:
            redis_keys = None
            print(f"[Redis] 연결 실패 — 건너뜀 ({e})")
        print("[Neo4j] 유저 데이터 없음 — 대상 없음")

        if dry_run:
            print("--dry-run: 삭제하지 않았습니다")
            return

        for _, model in _CHILD_TABLES:
            await db.execute(delete(model).where(model.user_id.in_(ids)))
        await db.execute(delete(User).where(User.id.in_(ids), User.is_guest.is_(True)))
        await db.commit()
        print(f"PostgreSQL 삭제 완료: 게스트 {len(ids)}명")

    if redis_keys:
        for db_no in sorted({d for d, _ in redis_keys}):
            get_redis(db_no).delete(*[k for d, k in redis_keys if d == db_no])
        print(f"Redis 삭제 완료: {len(redis_keys)}개")


def main() -> None:
    parser = argparse.ArgumentParser(description="게스트 계정과 연관 데이터 일괄 삭제")
    parser.add_argument("--dry-run", action="store_true", help="삭제 대상 건수만 출력")
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=0,
        help="생성된 지 N일보다 오래된 게스트만 (기본 0 = 전체)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.dry_run, args.older_than_days))


if __name__ == "__main__":
    main()
