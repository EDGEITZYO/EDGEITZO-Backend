"""홈 화면 API — 유저 정보, 최근 탐색, 최근 열람 논문."""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.redis import get_redis
from app.core.response import success_response
from app.models.journal import Journal
from app.models.paper import Paper
from app.models.recent_read import RecentRead
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.paper import PaperCardTrustBadge

router = APIRouter()

_KST           = timezone(timedelta(hours=9))
_REDIS_DB      = 7
_HISTORY_KEY   = "recent_searches:{user_id}"
_HISTORY_LIMIT = 10

_SEARCH_TERM_KEY   = "recent_search_terms:{user_id}"
_SEARCH_TERM_LIMIT = 6
_SEARCH_TERM_TTL   = 86400 * 7


# ── 스키마 ─────────────────────────────────────────────────────────────

class RecentSearchItem(BaseModel):
    id: str = Field(description="탐색 고유 ID")
    type: str = Field(description="탐색 유형. 'keyword': 키워드 탐색 (keyword_path 사용), 'ai': AI 탐색 (recommended_keywords 사용)", example="keyword")
    title: str = Field(description="탐색 제목. AI: 첫 발화 요약 / keyword: 노드명", example="딥러닝")
    map_session_id: Optional[str] = Field(None, description="키워드맵 세션 ID (type='keyword'일 때 사용, 그룹핑 기준)")
    last_viewed_paper_title: Optional[str] = Field(None, description="해당 탐색에서 마지막으로 열람한 논문 제목. 없으면 null", example="BERT를 활용한 자연어처리 연구")
    keyword_path: List[str] = Field(default=[], description="가장 최근 탐색 경로 (type='keyword'일 때 사용)", example=["AI", "딥러닝", "CNN"])
    keyword_paths: List[List[str]] = Field(default=[], description="누적된 모든 탐색 경로 목록 (type='keyword'일 때 사용)", example=[["AI", "딥러닝", "CNN"], ["AI", "딥러닝", "RNN"]])
    recommended_keywords: List[str] = Field(default=[], description="AI 탐색 추천 키워드 목록 (type='ai'일 때 사용)", example=["transformer", "attention", "BERT"])
    created_at: str = Field(description="탐색 생성/갱신 시각 (ISO8601)", example="2024-03-15T10:30:00+00:00")


class RecentPaperItem(BaseModel):
    paper_id: str = Field(description="논문 고유 ID", example="JAKO202312345678")
    paper_type: Optional[str] = Field(None, description="논문 유형. '박사학위 논문' | '석사학위 논문' | '학술 저널' | null", example="학술 저널")
    journal_name: Optional[str] = Field(None, description="학술지명. 없으면 null", example="한국정보과학회논문지")
    published_at: Optional[str] = Field(None, description="발행일 (ISO8601). pubdate 우선, 없으면 pubyear 기준 '{year}-01-01' 형태", example="2023-06-15")
    title: str = Field(description="논문 제목", example="BERT를 활용한 자연어처리 연구")
    keywords: List[str] = Field(description="논문 키워드 (최대 5개)", example=["BERT", "자연어처리", "딥러닝"])
    badges: PaperCardTrustBadge
    viewed_at: str = Field(description="열람 시각 (ISO8601)", example="2024-03-15T10:30:00+00:00")


class HomeResponse(BaseModel):
    user: dict = Field(description="로그인 유저 정보 (id, name, personalized_message)")
    recent_searches: List[RecentSearchItem] = Field(description="최근 탐색 이력 (최대 10건, 최신순)")
    recent_papers: List[RecentPaperItem] = Field(description="최근 열람 논문 (최대 10건, 최신순)")


class RecordReadRequest(BaseModel):
    paper_id: str = Field(description="열람한 논문 ID", example="JAKO202312345678")
    search_id: str | None = Field(default=None, description="연결된 탐색 세션 ID (recent_searches 갱신용)")


class SaveSearchTermRequest(BaseModel):
    term: str = Field(..., min_length=1, description="사용자가 검색한 검색어 원문. 연구자명 검색/연구 분야 검색 구분 없이 그대로 전달", example="딥러닝")


class RecentSearchTermsResponse(BaseModel):
    terms: List[str] = Field(description="최근 검색어 목록 (최대 6개, 최신 검색순). 동일 검색어(공백까지 완전 일치)는 1개만 포함됨", example=["딥러닝", "홍길동"])


# 헬퍼

_JOURNAL_DB_CODES = {"JAKO", "JAFO", "CFKO", "CFFO"}
_GREETING = [
    "오늘도 좋은 연구 하세요, {name}님!",
    "새로운 논문이 기다리고 있어요, {name}님.",
    "연구를 이어가볼까요, {name}님?",
]

def _personalized_message(name: str) -> str:
    return random.choice(_GREETING).format(name=name or "연구자")


# 엔드포인트

@router.get(
    "/home",
    summary="홈 화면 데이터",
    description="""로그인 유저의 홈 화면 데이터를 반환합니다. **Authorization 헤더에 Bearer 토큰 필요.**

**응답 최상위 구조**
- `success`: 요청 성공 여부 (bool)
- `message`: 응답 메시지
- `data`: 실제 데이터 (아래 구조)
- `meta`: 추가 메타 정보 (현재 빈 객체)

**data 구조**
- `user`: 유저 정보 (`id`, `name`, `personalized_message`)
- `recent_searches`: 최근 탐색 이력 최대 10건 (Redis, 7일 TTL)
  - `type`이 `keyword`이면 `keyword_path` 사용, `ai`이면 `recommended_keywords` 사용
- `recent_papers`: 최근 열람 논문 최대 10건 (DB, 최신순)
""",
    response_model=None,
)
async def get_home(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_id = str(current_user.id)

    # ── 최근 탐색 이력 (Redis) ─────────────────────────────────────────
    r = get_redis(_REDIS_DB)
    raw = r.get(_HISTORY_KEY.format(user_id=user_id))
    recent_searches: list[RecentSearchItem] = []
    if raw:
        for item in json.loads(raw):
            recent_searches.append(RecentSearchItem(**item))

    # ── 최근 열람 논문 (DB) ────────────────────────────────────────────
    # 논문당 가장 최근 read_at 1건만 조회 (날짜별 중복 제거)
    latest_per_paper = (
        select(
            RecentRead.paper_id,
            func.max(RecentRead.read_at).label("latest_read_at"),
        )
        .where(
            RecentRead.user_id == current_user.id,
            RecentRead.deleted_at.is_(None),
        )
        .group_by(RecentRead.paper_id)
        .order_by(desc("latest_read_at"))
        .limit(10)
        .subquery()
    )
    rows = (await db.execute(
        select(RecentRead, Paper, Journal)
        .join(Paper, RecentRead.paper_id == Paper.id)
        .outerjoin(Journal, Paper.journal_id == Journal.id)
        .join(
            latest_per_paper,
            (RecentRead.paper_id == latest_per_paper.c.paper_id)
            & (RecentRead.read_at == latest_per_paper.c.latest_read_at),
        )
        .where(RecentRead.user_id == current_user.id)
        .order_by(desc(RecentRead.read_at))
    )).all()

    recent_papers: list[RecentPaperItem] = []
    for read, paper, journal in rows:
        kws = paper.keywords_ko or []
        db_code = paper.db_code or ""
        if paper.pubdate:
            published_at = str(paper.pubdate).replace('.', '-')
        elif paper.pubyear:
            published_at = f"{paper.pubyear}-01-01"
        else:
            published_at = None

        degree = paper.degree or ""
        if db_code == "DIKO":
            if "박사" in degree:
                paper_type = "박사학위 논문"
            elif "석사" in degree:
                paper_type = "석사학위 논문"
            else:
                paper_type = "학위논문"
            degree_type = paper_type
        elif db_code in _JOURNAL_DB_CODES:
            paper_type = "학술 저널"
            degree_type = None
        else:
            paper_type = None
            degree_type = None

        recent_papers.append(RecentPaperItem(
            paper_id=paper.id,
            paper_type=paper_type,
            journal_name=journal.title if journal else None,
            published_at=published_at,
            title=paper.title or "",
            keywords=kws[:5],
            badges=PaperCardTrustBadge(
                kci=bool(journal.kci_indexed) if journal else (db_code == "JAKO"),
                sci=bool(journal.sci_indexed) if journal else False,
                citation_count=paper.citation_count or None,
                degree_type=degree_type,
            ),
            viewed_at=read.read_at.isoformat(),
        ))

    return success_response(
        data=HomeResponse(
            user={
                "id": user_id,
                "name": current_user.name or "",
                "personalized_message": _personalized_message(current_user.name or ""),
            },
            recent_searches=recent_searches,
            recent_papers=recent_papers,
        ),
        message="home data fetched",
    )


@router.post(
    "/home/recent-reads",
    summary="논문 열람 기록",
    description="""논문 상세 페이지 진입 시 호출. **Authorization 헤더에 Bearer 토큰 필요.**

- 최초 열람 시 `recent_reads` 테이블에 새 레코드 생성
- 재열람 시 `read_at` 갱신 및 `view_count` +1
- 존재하지 않는 `paper_id` 전달 시 404 반환
""",
)
async def record_read(
    request: RecordReadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)

    # paper 존재 확인
    paper = (await db.execute(
        select(Paper).where(Paper.id == request.paper_id)
    )).scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="논문을 찾을 수 없습니다.")

    now_kst = now.astimezone(_KST)
    today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    existing = (await db.execute(
        select(RecentRead).where(
            RecentRead.user_id == current_user.id,
            RecentRead.paper_id == request.paper_id,
            RecentRead.read_at >= today_start,
        )
    )).scalar_one_or_none()

    if existing:
        existing.read_at = now
        existing.deleted_at = None
        existing.view_count = (existing.view_count or 0) + 1
    else:
        db.add(RecentRead(
            id=uuid.uuid4(),
            user_id=current_user.id,
            paper_id=request.paper_id,
            read_at=now,
            view_count=1,
        ))

    await db.commit()

    if request.search_id:
        update_last_viewed(
            user_id=str(current_user.id),
            search_id=request.search_id,
            paper_title=paper.title,
        )

    return success_response(data={"recorded": True}, message="read recorded")


@router.post(
    "/home/recent-search-terms",
    response_model=ApiResponse[dict],
    summary="최근 검색어 저장",
    description="""검색 실행 시 호출해 최근 검색어를 저장합니다.

- 연구자명 검색/연구 분야 검색 구분 없이 하나의 목록에 저장됩니다.
- 동일 검색어(공백까지 완전 일치)가 이미 있으면 기존 항목을 지우고 맨 앞으로 옮깁니다 (중복 저장 안 함).
- 최대 6개까지만 유지되며, 초과되는 오래된 검색어는 잘려나갑니다.
""",
)
async def add_recent_search_term(
    request: SaveSearchTermRequest,
    current_user: User = Depends(get_current_user),
):
    save_search_term(user_id=str(current_user.id), term=request.term)
    return success_response(data={"saved": True}, message="recent search term saved")


@router.get(
    "/home/recent-search-terms",
    response_model=ApiResponse[RecentSearchTermsResponse],
    summary="최근 검색어 목록 조회",
    description="""홈에서 검색창 진입 시 호출. 검색창 하단에 칩 형태로 노출할 최근 검색어 목록을 반환합니다.

- 연구자명 검색/연구 분야 검색 구분 없이 최근 검색한 순서대로 최대 6개 반환
- 동일 검색어(공백까지 완전 일치)는 1개만 포함됨
- 칩 클릭 시 해당 검색어로 바로 검색을 실행하면 됨
""",
)
async def get_recent_search_terms(
    current_user: User = Depends(get_current_user),
):
    r = get_redis(_REDIS_DB)
    raw = r.get(_SEARCH_TERM_KEY.format(user_id=str(current_user.id)))
    terms: list[str] = json.loads(raw) if raw else []
    return success_response(
        data=RecentSearchTermsResponse(terms=terms),
        message="recent search terms fetched",
    )


def save_search_term(user_id: str, term: str) -> None:
    """검색 실행 시 호출 — Redis에 최근 검색어 저장 (최대 6건, 완전 일치 중복 제거).

    같은 검색어를 다시 검색하면 기존 항목을 제거하고 맨 앞으로 옮겨(순서만 갱신) 목록에는 항상 1개만 남는다.
    연구자명 검색/연구 분야 검색 구분 없이 동일한 목록에 쌓인다.
    """
    term = term.strip()
    if not term:
        return
    r = get_redis(_REDIS_DB)
    key = _SEARCH_TERM_KEY.format(user_id=user_id)
    existing: list[str] = json.loads(r.get(key) or "[]")
    existing = [t for t in existing if t != term]
    existing.insert(0, term)
    r.set(key, json.dumps(existing[:_SEARCH_TERM_LIMIT], ensure_ascii=False), ex=_SEARCH_TERM_TTL)


def save_search_history(
    user_id: str,
    search_type: str,       # "ai" | "keyword"
    title: str,             # AI: 첫 발화 요약 / keyword: 연구 분야명
    search_id: str,
    keyword_path: list[str] | None = None,          # keyword 탐색 breadcrumb (단일 경로)
    recommended_keywords: list[str] | None = None,  # AI 검색 추천 키워드
    last_viewed_paper_title: str | None = None,
    slots: dict | None = None,
    map_session_id: str | None = None,              # 키워드맵 세션 ID (그룹핑 기준)
) -> None:
    """검색 실행 후 호출 — Redis에 최근 탐색 이력 저장 (최대 10건 유지).

    keyword 탐색 + map_session_id 제공 시: 동일 세션 항목이 있으면 새 항목 추가 대신 기존 항목 갱신
    (keyword_paths 누적, 맨 앞으로 이동). 없으면 신규 항목 추가.
    """
    r = get_redis(_REDIS_DB)
    key = _HISTORY_KEY.format(user_id=user_id)
    existing = json.loads(r.get(key) or "[]")

    now = datetime.now(timezone.utc).isoformat()

    if search_type == "keyword" and map_session_id:
        matched = next((i for i in existing if i.get("map_session_id") == map_session_id), None)
        if matched:
            all_paths: list[list[str]] = list(matched.get("keyword_paths") or [])
            new_path = keyword_path or []
            if new_path and new_path not in all_paths:
                all_paths.append(new_path)
            matched["keyword_paths"] = all_paths
            matched["keyword_path"] = new_path
            matched["title"] = title
            matched["created_at"] = now
            if last_viewed_paper_title:
                matched["last_viewed_paper_title"] = last_viewed_paper_title
            existing = [i for i in existing if i.get("map_session_id") != map_session_id]
            existing.insert(0, matched)
            r.set(key, json.dumps(existing[:_HISTORY_LIMIT], ensure_ascii=False), ex=86400 * 7)
            return

    new_item = {
        "id": search_id,
        "type": search_type,
        "title": title,
        "map_session_id": map_session_id,
        "last_viewed_paper_title": last_viewed_paper_title,
        "keyword_path": keyword_path or [],
        "keyword_paths": [keyword_path] if keyword_path else [],
        "recommended_keywords": recommended_keywords or [],
        "slots": slots,
        "created_at": now,
    }
    existing = [i for i in existing if i.get("id") != search_id]
    existing.insert(0, new_item)
    r.set(key, json.dumps(existing[:_HISTORY_LIMIT], ensure_ascii=False), ex=86400 * 7)


def update_last_viewed(user_id: str, search_id: str, paper_title: str) -> None:
    """논문 열람 시 최근 탐색 이력의 last_viewed_paper_title 갱신."""
    r = get_redis(_REDIS_DB)
    key = _HISTORY_KEY.format(user_id=user_id)
    existing = json.loads(r.get(key) or "[]")
    for item in existing:
        if item.get("id") == search_id:
            item["last_viewed_paper_title"] = paper_title
            break
    r.set(key, json.dumps(existing, ensure_ascii=False), ex=86400 * 7)
