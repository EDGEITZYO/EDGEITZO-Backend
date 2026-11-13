"""홈 검색어 추천 칩 — 검색 필드 아래에 AI 검색 문장 1개 + 키워드 검색어 1개.

- 신규 사용자(열람 이력 없음): 온보딩 입력(연구 분야·논문 탐색 목적)으로 LLM이 생성한다.
- 기존 사용자: 최근 2주 열람 논문의 키워드를 열람 수로 집계하고, 많이 본 키워드 후보 중에서
  LLM이 키워드를 고르고 그 분야로 AI 검색 문장을 만든다. 최근 2주에 열람이 없으면
  마지막 열람일부터 거슬러 2주를 본다.

키워드 칩은 반드시 키워드 검색 DB(Neo4j Keyword 노드)에 있는 단어여야 한다 — 칩을 누르면
GET /keyword-map?key= 로 바로 들어가므로, DB에 없는 단어를 내주면 404가 난다.
그래서 LLM이 낸 단어를 그대로 쓰지 않고 항상 DB에서 확인한 노드의 이름·key를 돌려준다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.models.paper import Paper
from app.models.recent_read import RecentRead
from app.models.user import User
from app.services.keywords.keyword_db import (
    Keyword,
    get_keywords_by_keys,
    is_usable_anchor_name,
    keyword_key,
)
from app.services.keywords.keyword_embedding_search import embedding_search

logger = logging.getLogger(__name__)

# 사용자에게 문장 그대로 노출되는 글이라 quality 등급 (settings.llm_model_quality 주석 참고)
_MODEL = settings.llm_model_quality
_MAX_TOKENS = 400

AI_QUERY_MAX_LEN = 24          # 공백 포함 글자 수 상한 (기획)
HISTORY_WINDOW = timedelta(days=14)
_HISTORY_CANDIDATES = 5        # LLM에게 넘기는 '많이 본 키워드' 후보 수
_LLM_QUERY_CANDIDATES = 5      # 길이 조건을 못 지키는 경우가 있어 여러 개 받아 고른다
_LLM_KEYWORD_CANDIDATES = 5

BASIS_ONBOARDING = "onboarding"
BASIS_HISTORY = "history"
BASIS_NONE = "none"


@dataclass
class KeywordChip:
    name: str
    key: str


@dataclass
class SearchSuggestions:
    basis: str                       # onboarding | history | none
    ai_query: Optional[str]
    keyword: Optional[KeywordChip]
    history_start: Optional[datetime] = None
    history_end: Optional[datetime] = None


# ─────────────────────────────── 검증 ───────────────────────────────

_SENTENCE_END = re.compile(r"[.?!。？！]")


def normalize_ai_query(raw: object) -> Optional[str]:
    """LLM 후보 문장 1개를 기획 조건(한 문장, 공백 포함 24자 이하)에 맞는지 보고, 맞으면 정리해서 돌려준다."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split()).strip().strip("\"'“”‘’").strip()
    if not text or len(text) > AI_QUERY_MAX_LEN:
        return None
    # 한 문장: 문장 끝 부호가 마지막 자리 말고는 나오면 안 된다
    body = text.rstrip(".?!。？！ ")
    if _SENTENCE_END.search(body):
        return None
    return text


def is_single_word(name: Optional[str]) -> bool:
    """키워드 칩은 한 단어만 — 띄어쓰기가 있는 키워드 노드는 쓰지 않는다."""
    return bool(name) and len(name.split()) == 1 and is_usable_anchor_name(name)


def _pick_ai_query(candidates: object) -> Optional[str]:
    if isinstance(candidates, str):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return None
    for c in candidates:
        q = normalize_ai_query(c)
        if q:
            return q
    return None


def _first_json(raw: str) -> dict:
    start = raw.find("{")
    if start < 0:
        raise ValueError("JSON 객체를 찾지 못함")
    data, _ = json.JSONDecoder().raw_decode(raw[start:])
    return data if isinstance(data, dict) else {}


# ─────────────────────────────── 열람 이력 ───────────────────────────────

def history_window(last_read_at: datetime, now: datetime) -> tuple[datetime, datetime]:
    """집계 구간. 최근 2주에 열람이 있으면 [지금-2주, 지금], 없으면 [마지막 열람-2주, 마지막 열람]."""
    if last_read_at >= now - HISTORY_WINDOW:
        return now - HISTORY_WINDOW, now
    return last_read_at - HISTORY_WINDOW, last_read_at


def rank_history_keywords(rows: list[tuple[Optional[list[str]], int, datetime]]) -> list[tuple[str, str, int]]:
    """(논문 keywords_ko, 열람 수, 열람 시각) 행들 → [(표시 이름, key, 열람 수 합)], 많이 본 순.

    한 논문 안의 같은 키워드는 한 번만 센다. 열람 수가 같으면 더 최근에 본 쪽이 앞.
    """
    score: dict[str, int] = {}
    latest: dict[str, datetime] = {}
    shown: dict[str, str] = {}
    for keywords, views, read_at in rows:
        seen: set[str] = set()
        for kw in keywords or []:
            if not kw or not kw.strip():
                continue
            key = keyword_key(kw)
            if key in seen:
                continue
            seen.add(key)
            score[key] = score.get(key, 0) + max(views or 1, 1)
            if key not in latest or read_at > latest[key]:
                latest[key] = read_at
            shown.setdefault(key, " ".join(kw.split()))
    ordered = sorted(score, key=lambda k: (score[k], latest[k]), reverse=True)
    return [(shown[k], k, score[k]) for k in ordered]


async def _history_keywords(
    db: AsyncSession, user_id, now: datetime
) -> tuple[list[tuple[Keyword, int]], Optional[tuple[datetime, datetime]]]:
    """열람 이력 기반 키워드 후보 [(DB 키워드 노드, 열람 수)]와 집계 구간. 이력이 없으면 ([], None)."""
    base = (RecentRead.user_id == user_id, RecentRead.deleted_at.is_(None))
    last_read_at = (await db.execute(select(func.max(RecentRead.read_at)).where(*base))).scalar_one_or_none()
    if last_read_at is None:
        return [], None

    start, end = history_window(last_read_at, now)
    rows = (await db.execute(
        select(Paper.keywords_ko, RecentRead.view_count, RecentRead.read_at)
        .join(Paper, RecentRead.paper_id == Paper.id)
        .where(*base, RecentRead.read_at >= start, RecentRead.read_at <= end)
    )).all()
    ranked = [r for r in rank_history_keywords([tuple(r) for r in rows]) if is_single_word(r[0])]
    if not ranked:
        return [], (start, end)

    # DB에 없는 키워드가 섞여 있을 수 있어 넉넉히 확인한 뒤 앞에서부터 자른다
    probe = ranked[: _HISTORY_CANDIDATES * 4]
    found = await asyncio.to_thread(get_keywords_by_keys, [k for _, k, _ in probe])
    picked = [(found[k], views) for _, k, views in probe if k in found]
    return picked[:_HISTORY_CANDIDATES], (start, end)


# ─────────────────────────────── LLM ───────────────────────────────

# 서비스가 다루는 논문 범위. 온보딩 연구 분야는 자유 입력이라 범위 밖 분야가 들어올 수 있는데,
# 그대로 생성하면 DB에 없는 주제의 문장·키워드가 나온다(키워드는 엉뚱한 노드로 대체됨).
SERVICE_DOMAIN = "생명공학"

_SYSTEM = f"""너는 학술 논문 검색 서비스의 홈 화면에 띄울 '추천 검색어'를 만든다. JSON만 출력하고 다른 텍스트는 쓰지 않는다.
이 서비스의 논문 DB는 {SERVICE_DOMAIN} 분야 논문으로 구성되어 있다. 추천 검색어는 반드시 {SERVICE_DOMAIN} 분야 안의 주제로 만든다.
사용자의 연구 분야가 {SERVICE_DOMAIN}과 거리가 있으면, 그 분야와 {SERVICE_DOMAIN}이 만나는 주제로 해석한다.

ai_queries 규칙 (AI 검색창에 사용자가 그대로 입력할 문장):
- 사용자가 AI에게 논문을 찾아 달라고 묻거나 요청하는 반말 구어체 한 문장 ("~ 논문을 찾고 싶어", "~ 알려줘", "~ 있을까?" 같은 어조)
- 한 문장만. 문장 안에 마침표·물음표로 끊긴 두 번째 문장을 넣지 않는다
- 공백과 문장부호를 포함해 {AI_QUERY_MAX_LEN}자 이하. 넘으면 주제를 줄여서 맞춘다
- 전달받은 연구 분야·키워드와 직접 관련된 구체적인 연구 주제를 담는다. 없는 사실을 지어내지 않는다"""

_ONBOARDING_INSTRUCTION = f"""아래는 신규 사용자가 온보딩에서 입력한 정보다.
{{profile}}

다음 JSON을 출력하라.
{{{{"ai_queries": [후보 문장 {_LLM_QUERY_CANDIDATES}개, 가장 좋은 것부터], "keywords": [키워드 후보 {_LLM_KEYWORD_CANDIDATES}개, 가장 좋은 것부터]}}}}

keywords 규칙: 이 사용자의 연구 분야에서 키워드 검색에 쓸 학술 용어. 띄어쓰기 없는 한 단어의 한국어 명사. 논문 키워드로 흔히 쓰이는 표준 용어를 고른다."""

_HISTORY_INSTRUCTION = f"""아래는 이 사용자가 최근 2주 동안 열람한 논문의 키워드를 열람 수가 많은 순으로 정리한 목록이다.
{{candidates}}

다음 JSON을 출력하라.
{{{{"keyword": 목록에서 고른 키워드 1개(목록에 적힌 그대로), "ai_queries": [후보 문장 {_LLM_QUERY_CANDIDATES}개, 가장 좋은 것부터]}}}}

keyword 규칙: 열람 수가 가장 많은 키워드를 우선한다. 단, 그 키워드가 '연구', '분석'처럼 분야를 나타내지 못하는 일반 단어면 다음 순위를 고른다.
ai_queries 규칙: 고른 keyword의 분야에서 사용자가 이어서 찾아볼 만한 주제로 만든다."""


async def _ask_llm(prompt: str, label: str) -> Optional[dict]:
    from app.services.llm.client import LLMBudgetExceededError, LLMRefusalError, chat

    try:
        resp = await chat(
            messages=[{"role": "user", "content": prompt}],
            model=_MODEL,
            system=_SYSTEM,
            temperature=0.3,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "disabled"},
            # 입력(온보딩 값 또는 후보 목록)이 같으면 같은 답을 재사용한다 — 홈 진입마다 호출되므로
            use_cache=True,
        )
        return _first_json(resp.text)
    except LLMBudgetExceededError:
        logger.warning("홈 추천 검색어(%s): LLM 예산 소진", label)
    except LLMRefusalError:
        logger.warning("홈 추천 검색어(%s): 모델이 응답을 거부", label)
    except Exception as exc:
        logger.warning("홈 추천 검색어(%s) 실패: %s", label, type(exc).__name__)
    return None


def _profile_text(user: User) -> Optional[str]:
    lines: list[str] = []
    if user.research_field and user.research_field.strip():
        lines.append(f"- 연구 분야: {user.research_field.strip()}")
    purposes = [p for p in (user.purposes or []) if isinstance(p, str) and p.strip() and p != "기타"]
    if user.purpose_custom and user.purpose_custom.strip():
        purposes.append(user.purpose_custom.strip())
    if purposes:
        lines.append(f"- 논문 탐색 목적: {', '.join(purposes)}")
    # 연구 분야가 없으면 목적만으로는 주제를 정할 수 없다
    if not any(line.startswith("- 연구 분야") for line in lines):
        return None
    return "\n".join(lines)


def _resolve_keyword_sync(candidates: list[str]) -> Optional[Keyword]:
    """LLM 키워드 후보 → DB에 있는 키워드 노드. 이름이 정확히 같은 노드를 우선하고,
    하나도 없으면 후보 순서대로 임베딩 검색으로 의미가 가까운 노드를 찾는다."""
    words = [c.strip() for c in candidates if isinstance(c, str) and is_single_word(c.strip())]
    if not words:
        return None
    found = get_keywords_by_keys([keyword_key(w) for w in words])
    for w in words:
        kw = found.get(keyword_key(w))
        if kw is not None:
            return kw
    # search_keywords()를 쓰지 않는 이유: 다 실패하면 검색어를 '사용자가 찾다 실패한 말'로
    # 실패 로그(동의어 사전 보강용)에 남기는데, LLM 후보는 사용자 입력이 아니라 로그를 오염시킨다.
    for w in words:
        for meta in embedding_search(w, lang="ko", limit=5):
            if meta.get("key") and is_single_word(meta.get("name")):
                return Keyword(key=meta["key"], name_ko=meta["name"], name_en=None,
                               paper_count=meta.get("paper_count", 0))
    return None


def _chip(kw: Optional[Keyword]) -> Optional[KeywordChip]:
    if kw is None:
        return None
    name = kw.name_ko or kw.name_en
    return KeywordChip(name=name, key=kw.key) if name else None


async def _from_onboarding(user: User) -> SearchSuggestions:
    profile = _profile_text(user)
    if profile is None:
        return SearchSuggestions(basis=BASIS_NONE, ai_query=None, keyword=None)

    data = await _ask_llm(_ONBOARDING_INSTRUCTION.format(profile=profile), "onboarding") or {}
    raw_keywords = data.get("keywords")
    keyword = None
    if isinstance(raw_keywords, list):
        try:
            keyword = await asyncio.to_thread(_resolve_keyword_sync, raw_keywords)
        except Exception as exc:
            logger.warning("홈 추천 검색어: 키워드 DB 확인 실패: %s", type(exc).__name__)
    return SearchSuggestions(
        basis=BASIS_ONBOARDING,
        ai_query=_pick_ai_query(data.get("ai_queries")),
        keyword=_chip(keyword),
    )


async def _from_history(
    candidates: list[tuple[Keyword, int]], window: tuple[datetime, datetime]
) -> SearchSuggestions:
    lines = [f"{i}. {kw.name_ko or kw.name_en} (열람 {views}회)" for i, (kw, views) in enumerate(candidates, 1)]
    data = await _ask_llm(_HISTORY_INSTRUCTION.format(candidates="\n".join(lines)), "history") or {}

    # LLM이 고른 키워드가 후보 목록 밖이면 버리고 열람 수 1위를 쓴다
    by_key = {kw.key: kw for kw, _ in candidates}
    chosen = data.get("keyword")
    picked = by_key.get(keyword_key(chosen)) if isinstance(chosen, str) and chosen.strip() else None
    keyword = picked or candidates[0][0]
    return SearchSuggestions(
        basis=BASIS_HISTORY,
        ai_query=_pick_ai_query(data.get("ai_queries")),
        keyword=_chip(keyword),
        history_start=window[0],
        history_end=window[1],
    )


async def get_search_suggestions(db: AsyncSession, user: User, now: Optional[datetime] = None) -> SearchSuggestions:
    now = now or datetime.now(timezone.utc)
    try:
        candidates, window = await _history_keywords(db, user.id, now)
    except Exception as exc:
        # Neo4j 장애 등으로 이력 확인이 안 되면 온보딩 기준으로라도 보여준다
        logger.warning("홈 추천 검색어: 열람 이력 집계 실패: %s", type(exc).__name__)
        candidates, window = [], None
    if candidates and window is not None:
        return await _from_history(candidates, window)
    # 열람 이력이 없거나, 열람한 논문에 쓸 만한 키워드가 하나도 없으면 온보딩 기준
    return await _from_onboarding(user)
