"""홈 화면 API — 유저 정보, 최근 탐색, 최근 열람 논문."""
from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.redis import get_redis
from app.core.response import success_response
from app.models.paper import Paper
from app.models.recent_read import RecentRead
from app.models.user import User

router = APIRouter()

_REDIS_DB      = 7
_HISTORY_KEY   = "recent_searches:{user_id}"
_HISTORY_LIMIT = 10


# ── 스키마 ─────────────────────────────────────────────────────────────

class RecentSearchItem(BaseModel):
    id: str
    type: str                               # "keyword" | "ai"
    title: str
    last_viewed_paper_title: Optional[str]  # 마지막으로 열람한 논문 제목
    keyword_path: List[str]                 # 키워드 탐색 breadcrumb (keyword 타입)
    recommended_keywords: List[str]         # AI 검색 추천 키워드 (ai 타입)
    created_at: str


class PaperBadges(BaseModel):
    kci: Optional[str]          # "O" | "X" | null
    citation_count: Optional[int]


class RecentPaperItem(BaseModel):
    paper_id: str
    paper_type: Optional[str]
    journal_name: Optional[str]
    published_at: Optional[str]  # ISO8601 (발행일 or 발행연도-01-01)
    title: str
    keywords: List[str]
    badges: PaperBadges
    viewed_at: str


class HomeResponse(BaseModel):
    user: dict
    recent_searches: List[RecentSearchItem]
    recent_papers: List[RecentPaperItem]


class RecordReadRequest(BaseModel):
    paper_id: str


# ── 헬퍼 ───────────────────────────────────────────────────────────────

_PAPER_TYPE_MAP = {"JAKO": "저널", "JAFO": "저널", "DIKO": "학위논문", "CFKO": "학회"}
_GREETING = [
    "오늘도 좋은 연구 하세요, {name}님!",
    "새로운 논문이 기다리고 있어요, {name}님.",
    "연구를 이어가볼까요, {name}님?",
]

import random


def _personalized_message(name: str) -> str:
    return random.choice(_GREETING).format(name=name or "연구자")


# ── 엔드포인트 ──────────────────────────────────────────────────────────

@router.get(
    "/home",
    summary="홈 화면 데이터",
    description="로그인 유저의 개인화 메시지, 최근 탐색 이력(최대 10건), 최근 열람 논문(최대 10건) 반환.",
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
    rows = (await db.execute(
        select(RecentRead, Paper)
        .join(Paper, RecentRead.paper_id == Paper.id)
        .where(
            RecentRead.user_id == current_user.id,
            RecentRead.deleted_at.is_(None),
        )
        .order_by(desc(RecentRead.read_at))
        .limit(10)
    )).all()

    recent_papers: list[RecentPaperItem] = []
    for read, paper in rows:
        kws = paper.keywords_ko or []
        db_code = paper.db_code or ""
        # published_at: pubdate 우선, 없으면 pubyear로 연도만 ISO8601 변환
        if paper.pubdate:
            published_at = str(paper.pubdate).replace('.', '-')
        elif paper.pubyear:
            published_at = f"{paper.pubyear}-01-01"
        else:
            published_at = None
        recent_papers.append(RecentPaperItem(
            paper_id=paper.id,
            paper_type=_PAPER_TYPE_MAP.get(db_code),
            journal_name=paper.journal.title if paper.journal else None,
            published_at=published_at,
            title=paper.title or "",
            keywords=kws[:5],
            badges=PaperBadges(
                kci="O" if db_code == "JAKO" else ("X" if db_code else None),
                citation_count=paper.citation_count or None,
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
    description="논문 상세 페이지 진입 시 호출. recent_reads 테이블에 기록 (중복 시 read_at 갱신).",
)
async def record_read(
    request: RecordReadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # paper 존재 확인
    paper = (await db.execute(
        select(Paper).where(Paper.id == request.paper_id)
    )).scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="논문을 찾을 수 없습니다.")

    existing = (await db.execute(
        select(RecentRead).where(
            RecentRead.user_id == current_user.id,
            RecentRead.paper_id == request.paper_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.read_at = now
        existing.deleted_at = None
        existing.view_count = (existing.view_count or 0) + 1
    else:
        import uuid
        db.add(RecentRead(
            id=uuid.uuid4(),
            user_id=current_user.id,
            paper_id=request.paper_id,
            read_at=now,
            view_count=1,
        ))

    await db.commit()
    return success_response(data={"recorded": True}, message="read recorded")


def save_search_history(
    user_id: str,
    search_type: str,       # "ai" | "keyword"
    title: str,             # AI: 첫 발화 요약 / keyword: 노드명
    search_id: str,
    keyword_path: list[str] | None = None,          # keyword 탐색 breadcrumb
    recommended_keywords: list[str] | None = None,  # AI 검색 추천 키워드
    last_viewed_paper_title: str | None = None,
    slots: dict | None = None,
) -> None:
    """검색 실행 후 호출 — Redis에 최근 탐색 이력 저장 (최대 10건 유지)."""
    from datetime import datetime, timezone

    r = get_redis(_REDIS_DB)
    key = _HISTORY_KEY.format(user_id=user_id)
    existing = json.loads(r.get(key) or "[]")

    new_item = {
        "id": search_id,
        "type": search_type,
        "title": title,
        "last_viewed_paper_title": last_viewed_paper_title,
        "keyword_path": keyword_path or [],
        "recommended_keywords": recommended_keywords or [],
        "slots": slots,
        "created_at": datetime.now(timezone.utc).isoformat(),
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
