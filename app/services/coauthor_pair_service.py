"""공저자 페이지 상단 — 두 연구자 프로필·함께 작성한 논문 수·공동 연구 기간·공동 연구 키워드.

공동 연구 키워드 = 두 사람이 함께 쓴 논문들 사이에서 겹치는 키워드. 저자가 논문에 단 키워드에서만 뽑는다.
  1. 함께 쓴 논문이 1편이면 그 논문의 키워드를 그대로 쓴다(그 논문 자체가 공동 연구다). LLM을 부르지 않는다
  2. 2편 이상이면 2편 이상에 겹쳐 달린 키워드부터 채운다
  3. 그래도 표시 개수보다 적으면 남은 칸만 LLM이 '함께 쓴 논문에 실제로 달린 키워드' 중에서 골라 채운다.
     LLM은 후보 번호만 돌려주고, 번호가 후보 범위를 벗어나면 버린다 — 지어낸 말이 섞일 수 없다
  4. 2편 이상인데 고른 키워드에 한글과 영문이 섞여 있으면, LLM이 한글·영문 번역쌍을 짝짓고
     짝지어진 것은 한 칸으로 합친다(한글 표기). 빈 칸은 다음 순위로 채운다

2026-11-07 실측(공저 쌍 15,556):
- 겹치는 키워드로 3칸이 차는 쌍은 2편 15% / 3~5편 31% / 6편 이상 83%. 임베딩으로 서로 다른 논문의 뜻이 같은 키워드까지
  합쳐도 +0~4%p에 그쳐(임계값 0.85에서 'Hepatitis B virus↔C virus' 오병합) 의미 병합으로 겹침을 늘리지는 않는다
- KCI는 논문마다 한글·영문 키워드를 같이 달아 겹치는 키워드 상위 3개에 번역쌍('육계'·'broiler')이 섞이는 쌍이
  2편 13% / 3~5편 7% / 6편 이상 5%다. 임베딩(BGE-m3-ko)은 짧은 한글 용어의 번역쌍 유사도가 낮고(육계↔broiler 0.33,
  고추↔Hot pepper 0.48) 무관한 공출현이 0.48~0.54라 가를 수 없고, KCI 키워드 순서로 짝짓기는 표본 6편 중 2편이
  어긋나 쓸 수 없었다. 그래서 4단계는 LLM이 판단한다
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.schemas.researcher_detail import (
    COMMON_KEYWORD_LIMIT_MAX,
    CoauthorPairPerson,
    CoauthorPairSummaryResponse,
    CommonKeyword,
    ResearcherProfileResponse,
)
from app.services.researcher_detail_service import (
    _as_list,
    _department_display,
    _display_name,
    _paper_key,
    _sort_rows,
    fetch_paper_rows,
    get_profile,
)
from app.services.researcher_flow_service import _spread
from app.services.year_range import year_bounds

logger = logging.getLogger(__name__)

# 프롬프트·후보 규칙이 바뀌면 올린다 — 캐시 키가 바뀌어 자동으로 다시 고른다.
PROMPT_VERSION = "v4"
_MODEL = settings.llm_model_fast  # 모자란 칸 채우기
_PAIR_MODEL = settings.llm_model_quality  # 한글·영문 번역쌍 확인 — _same_meaning_groups 참고
# 함께 쓴 논문 몇 편에 달려야 '겹치는' 키워드로 보는지.
_COMMON_KEYWORD_MIN_PAPERS = 2
# LLM에게 보여줄 함께 쓴 논문 제목 수. 연구 흐름(분야 하나 10편)과 같은 양이다.
_TITLES_FOR_PROMPT = 10
_MAX_TOKENS = 200
_HANGUL = re.compile(r"[가-힣]")


def _pair_person(person: ResearcherProfileResponse) -> CoauthorPairPerson:
    return CoauthorPairPerson(
        researcher_id=person.researcher_id,
        name=_display_name(person),
        institution=person.institution,
        department=person.department,
        **_department_display(person.department, person.institution),
    )


def _keyword_key(keyword: str) -> str:
    """표기 흔들림(대소문자·띄어쓰기·전각)만 같은 키워드로 본다."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", keyword)).lower()


def _is_hangul(keyword: str) -> bool:
    return bool(_HANGUL.search(keyword))


def _tally(rows: list[Any]) -> tuple[dict[str, set[str]], dict[str, str]]:
    """(키워드 → 달린 논문 키 집합, 키워드 → 표시 문자열). 표시 dict의 순서는 최신 논문에서 먼저 나온 순이다.
    한 논문에 같은 키워드가 두 번 달려도 집합이라 한 편으로 센다."""
    papers: dict[str, set[str]] = {}
    shown: dict[str, str] = {}
    for row in _sort_rows(rows, "recent"):
        for keyword in _as_list(row.keywords):
            key = _keyword_key(keyword)
            if not key:
                continue
            papers.setdefault(key, set()).add(_paper_key(row))
            shown.setdefault(key, keyword)
    return papers, shown


def _paper_signature(rows: list[Any]) -> str:
    """캐시 무효화 키. 함께 쓴 논문이 늘거나 줄거나, 논문 키워드가 바뀌면(KCI 재적재 등) 달라진다."""
    parts = sorted(
        f"{_paper_key(r)}:{'|'.join(sorted(_keyword_key(k) for k in _as_list(r.keywords)))}" for r in rows
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


# ─────────────────────────────── LLM 공통 ───────────────────────────────

def _first_json(raw: str) -> dict:
    """첫 JSON 객체만 읽는다. 지시해도 모델이 JSON 뒤에 '선정 근거'를 덧붙이는 경우가 있어(실측, max_tokens에서 잘림)
    마지막 '}'까지 자르면 설명 속 괄호가 섞이거나 잘린 꼬리 때문에 통째로 실패한다."""
    start = raw.find("{")
    if start < 0:
        raise ValueError("JSON 객체를 찾지 못함")
    data, _ = json.JSONDecoder().raw_decode(raw[start:])
    return data


def _numbers(values: Any, count: int) -> list[int]:
    """목록 번호(1부터) → 0부터의 자리. 범위 밖·중복 번호는 버린다 — 목록 밖의 값이 섞이지 않게."""
    picked: list[int] = []
    for value in values or []:
        match = re.search(r"\d+", str(value))
        if not match:
            continue
        idx = int(match.group()) - 1
        if 0 <= idx < count and idx not in picked:
            picked.append(idx)
    return picked


async def _ask(
    system: str, prompt: str, label: str, *, model: str = _MODEL, thinking: Optional[dict] = None
) -> Optional[tuple[dict, str]]:
    """(JSON, 모델명). 예산 소진·거부·실패면 None — 호출부는 캐시하지 않아 다음 요청에 다시 시도한다."""
    from app.services.llm.client import LLMBudgetExceededError, LLMRefusalError, chat

    try:
        response = await chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            system=system,
            cache_system=True,
            temperature=0.0,
            max_tokens=_MAX_TOKENS,
            thinking=thinking,
        )
        return _first_json(response.text), response.model
    except LLMBudgetExceededError:
        logger.warning("공동 연구 키워드(%s): 이번 달 LLM 예산 소진", label)
    except LLMRefusalError:
        logger.warning("공동 연구 키워드(%s): 모델이 응답을 거부", label)
    except Exception as exc:  # 파싱 실패·네트워크 오류 등
        logger.warning("공동 연구 키워드(%s) 실패: %s", label, type(exc).__name__)
    return None


# ─────────────────────────────── 캐시 ───────────────────────────────
# 한 쌍에 한 행. ranked_keywords(3단계)와 keyword_groups(4단계)는 따로 채워지고, 논문 서명이 바뀌면 둘 다 버린다.

_CACHE_COLUMNS = ("ranked_keywords", "keyword_groups")


async def _load_cache(db: AsyncSession, pair: tuple[str, str], signature: str) -> dict:
    row = (
        await db.execute(
            text(
                "SELECT ranked_keywords, keyword_groups, paper_signature FROM coauthor_keyword_cache "
                "WHERE researcher_a = :a AND researcher_b = :b AND prompt_version = :ver"
            ),
            {"a": pair[0], "b": pair[1], "ver": PROMPT_VERSION},
        )
    ).first()
    if row is None or row.paper_signature != signature:
        return {}
    return {"ranked_keywords": row.ranked_keywords, "keyword_groups": row.keyword_groups}


async def _save_cache(
    db: AsyncSession, pair: tuple[str, str], signature: str, column: str, value: Any, model: Optional[str]
) -> None:
    if column not in _CACHE_COLUMNS:
        raise ValueError(column)
    other = next(c for c in _CACHE_COLUMNS if c != column)
    try:
        await db.execute(
            text(
                "INSERT INTO coauthor_keyword_cache "
                f"  (researcher_a, researcher_b, prompt_version, paper_signature, {column}, model) "
                "VALUES (:a, :b, :ver, :sig, CAST(:value AS jsonb), :model) "
                "ON CONFLICT (researcher_a, researcher_b, prompt_version) DO UPDATE SET "
                f"  {column} = EXCLUDED.{column}, "
                # 같은 논문 집합에서 먼저 저장한 다른 단계 결과는 남기고, 논문이 바뀌었으면 버린다.
                f"  {other} = CASE WHEN coauthor_keyword_cache.paper_signature = EXCLUDED.paper_signature "
                f"              THEN coauthor_keyword_cache.{other} END, "
                "  paper_signature = EXCLUDED.paper_signature, model = EXCLUDED.model, created_at = now()"
            ),
            {
                "a": pair[0], "b": pair[1], "ver": PROMPT_VERSION, "sig": signature,
                "value": json.dumps(value, ensure_ascii=False), "model": model,
            },
        )
        await db.commit()
    except Exception:
        logger.exception("공동 연구 키워드 캐시 저장 실패 — 응답에는 영향 없음")
        await db.rollback()


# ─────────────────────────────── 3단계: 모자란 칸 채우기 ───────────────────────────────

def _rank_system_prompt() -> str:
    return (
        "너는 두 연구자가 함께 쓴 논문들을 보고, 두 사람의 공동 연구를 대표하는 키워드를 고르는 편집자다.\n"
        "키워드는 반드시 [후보] 목록에서 번호로만 고른다. 새 단어를 만들거나 표기를 고치지 않는다.\n\n"
        "고르는 기준:\n"
        "1. 함께 쓴 논문들의 제목에 드러나는 공동 연구의 주제·대상·방법을 가리키는 키워드를 앞에 둔다.\n"
        "2. [이미 고른 키워드]와 뜻이 같은 후보(한글·영문 표기만 다른 것 포함)는 고르지 않는다.\n"
        "3. 후보끼리 뜻이 같으면(한글·영문 표기만 다른 것 포함) 하나만 고르고, 한글 후보를 고른다.\n"
        "4. 분야를 구분하지 못하는 일반적인 말(연구, 분석, 효과, 한국, Korea 등)은 고르지 않는다.\n"
        "5. 대표성이 높은 순서로, 지시한 개수까지만 고른다. 마땅한 후보가 적으면 적게 골라도 된다.\n\n"
        '출력은 JSON 한 줄만: {"picks": [후보 번호, ...]}\n'
        "선정 이유·설명·코드블록 표시는 쓰지 않는다."
    )


def _rank_prompt(rows: list[Any], chosen: list[str], candidates: list[str], pick_count: int) -> str:
    recent = [r for r in _sort_rows(rows, "recent") if r.title]
    titles = [f"({r.pubyear}) {r.title}" if r.pubyear else r.title for r in _spread(recent, _TITLES_FOR_PROMPT)]
    return (
        f"두 연구자가 함께 쓴 논문 {len(rows)}편 중 제목 {len(titles)}편:\n"
        + "\n".join(f"- {t}" for t in titles)
        + f"\n\n[이미 고른 키워드] {', '.join(chosen) if chosen else '없음'}\n\n[후보]\n"
        + "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
        + f"\n\n후보에서 최대 {pick_count}개를 골라라."
    )


def _parse_picks(raw: str, candidate_count: int) -> list[int]:
    return _numbers(_first_json(raw).get("picks"), candidate_count)


async def _ai_ranking(
    db: AsyncSession,
    pair: tuple[str, str],
    rows: list[Any],
    chosen: list[str],
    candidates: list[str],
) -> Optional[list[str]]:
    """후보 중 LLM이 고른 키워드(표시 문자열, 대표성 높은 순). LLM을 못 쓰면 None.

    표시 개수와 무관하게 상한(COMMON_KEYWORD_LIMIT_MAX)까지 고르게 해, keyword_limit이 달라도 캐시 하나로 채운다.
    """
    signature = _paper_signature(rows)
    cached = (await _load_cache(db, pair, signature)).get("ranked_keywords")
    if cached is not None:
        return list(cached)
    answer = await _ask(
        _rank_system_prompt(),
        _rank_prompt(rows, chosen, candidates, min(COMMON_KEYWORD_LIMIT_MAX, len(candidates))),
        "채우기",
    )
    if answer is None:
        return None
    data, model = answer
    ranked = [candidates[i] for i in _numbers(data.get("picks"), len(candidates))]
    await _save_cache(db, pair, signature, "ranked_keywords", ranked, model)
    return ranked


# ─────────────────────────────── 4단계: 뜻이 같은 키워드 묶기 ───────────────────────────────

def _pair_system_prompt() -> str:
    # 예시는 품질 비교(2026-11-07)에 쓴 키워드 목록과 겹치지 않는 것으로 골랐다.
    return (
        "너는 학술 논문 키워드의 한글·영문 번역쌍을 찾는 편집자다.\n"
        "[한글]의 키워드와 [영문]의 키워드 중, 영문이 그 한글 키워드를 그대로 옮긴 번역어(약어·화학식 포함)일 때만 짝짓는다.\n"
        "  짝짓는 예) '미토콘드리아 유전체'–'mitochondrial genome', '관리'–'management', '음악치료'–'music therapy'\n"
        "  짝짓지 않는 예) '수질 예측 모형'–'rainfall data'(다른 개념), '항균 활성'–'antifungal activity'(다른 개념), "
        "'B형 간염'–'Hepatitis C virus', '운동'–'aerobic exercise'(범위가 다름)\n"
        "같은 논문에 함께 달렸거나 관련이 깊다는 이유로 짝짓지 않는다. 확실하지 않으면 짝짓지 않는다. "
        "한 키워드는 한 쌍에만 쓴다.\n\n"
        '출력은 JSON 한 줄만: {"pairs": [[한글 번호, 영문 번호], ...]} — 없으면 {"pairs": []}. 설명은 쓰지 않는다.'
    )


def _pair_prompt(korean: list[str], english: list[str]) -> str:
    return (
        "[한글]\n" + "\n".join(f"{i}. {k}" for i, k in enumerate(korean, start=1))
        + "\n\n[영문]\n" + "\n".join(f"{i}. {k}" for i, k in enumerate(english, start=1))
    )


def _parse_pairs(data: dict, korean_count: int, english_count: int) -> list[tuple[int, int]]:
    """[한글 번호, 영문 번호] → 0부터의 자리. 범위 밖은 버리고, 한 키워드가 두 쌍에 나오면 먼저 나온 쌍만 쓴다
    (실측: 모델이 '도가'를 'Taoism'과 짝지은 뒤 다른 영문과 또 짝지은 경우가 있었다)."""
    used_ko: set[int] = set()
    used_en: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for item in data.get("pairs") or []:
        if not isinstance(item, list) or len(item) != 2:
            continue
        ko = _numbers([item[0]], korean_count)
        en = _numbers([item[1]], english_count)
        if not ko or not en or ko[0] in used_ko or en[0] in used_en:
            continue
        used_ko.add(ko[0])
        used_en.add(en[0])
        pairs.append((ko[0], en[0]))
    return pairs


async def _same_meaning_groups(
    db: AsyncSession, pair: tuple[str, str], rows: list[Any], keywords: list[str]
) -> dict[str, int]:
    """키워드 키 → 묶음 번호. 같은 번호끼리 한글·영문 번역쌍이다. LLM을 못 쓰면 빈 dict(합치지 않음).

    품질 비교(2026-11-07, 한글·영문이 섞인 목록 30개 + 앞선 5개): Haiku 4.5는 번역쌍을 7개 놓치고 다른 개념을 2번 묶었고
    (항산화효소↔SOD, 비티↔Frankliniella occidentalis), Sonnet 5는 1개 놓치고 1번 묶었다. 잘못 묶으면 서로 다른 키워드가
    사라지므로 품질 모델을 쓴다. 같은 질문을 '뜻이 같은 것을 묶어라'로 하면 두 모델 모두 관련 개념까지 묶었다
    (nitric oxide↔oxidative stress) — 그래서 한글과 영문을 나눠 보여 주고 번역쌍만 묻는다.
    """
    signature = _paper_signature(rows)
    cached = (await _load_cache(db, pair, signature)).get("keyword_groups")
    # 같은 논문 집합이어도 앞 단계(3단계 캐시·keyword_limit)에 따라 물어본 목록이 달라질 수 있어 목록까지 맞아야 쓴다.
    if cached is not None and cached.get("keywords") == keywords:
        groups = cached["groups"]
    else:
        korean = [k for k in keywords if _is_hangul(k)]
        english = [k for k in keywords if not _is_hangul(k)]
        answer = await _ask(
            _pair_system_prompt(), _pair_prompt(korean, english), "중복 확인",
            model=_PAIR_MODEL, thinking={"type": "disabled"},
        )
        if answer is None:
            return {}
        data, model = answer
        groups = [[korean[k], english[e]] for k, e in _parse_pairs(data, len(korean), len(english))]
        await _save_cache(db, pair, signature, "keyword_groups", {"keywords": keywords, "groups": groups}, model)
    return {_keyword_key(k): gid for gid, group in enumerate(groups) for k in group}


# ─────────────────────────────── 칸 채우기 ───────────────────────────────

def _pick(
    order: list[tuple[str, str]],
    papers: dict[str, set[str]],
    shown: dict[str, str],
    limit: int,
    groups: dict[str, int],
) -> list[dict]:
    """우선순위 목록을 limit칸에 담는다. 같은 묶음(뜻이 같음)이면 칸을 새로 쓰지 않고 앞 칸에 합친다 —
    편수는 합집합, 표기는 한글이 있으면 한글(화면이 한글이라서)."""
    items: list[dict] = []
    slot_of: dict[int, dict] = {}
    for key, source in order:
        gid = groups.get(key)
        item = slot_of.get(gid) if gid is not None else None
        if item is not None:
            item["papers"] |= papers[key]
            if _is_hangul(shown[key]) and not _is_hangul(item["display"]):
                item["display"] = shown[key]
            continue
        if len(items) >= limit:
            continue  # 칸은 찼지만 이미 담은 키워드와 같은 묶음이면 위에서 합쳐야 해 끝까지 훑는다
        item = {"display": shown[key], "papers": set(papers[key]), "source": source}
        items.append(item)
        if gid is not None:
            slot_of[gid] = item
    return items


def _mixed_scripts(items: list[dict]) -> bool:
    scripts = {_is_hangul(i["display"]) for i in items}
    return len(scripts) > 1


async def _fill(
    db: AsyncSession, pair: tuple[str, str], rows: list[Any], repeated: list[str], candidates: list[str],
    shown: dict[str, str],
) -> list[tuple[str, str]]:
    """3단계. LLM이 고른 후보(ai), LLM을 못 쓰면 최신 논문 순 후보(recent)."""
    ranked = await _ai_ranking(db, pair, rows, [shown[k] for k in repeated], [shown[k] for k in candidates])
    if ranked is None:
        return [(k, "recent") for k in candidates]
    keys = [_keyword_key(k) for k in ranked]
    # LLM이 마땅한 후보가 적다고 판단해 덜 골랐으면 칸을 억지로 채우지 않는다.
    return [(k, "ai") for k in dict.fromkeys(keys) if k in shown and k not in repeated]


def _result(items: list[dict]) -> list[CommonKeyword]:
    return [CommonKeyword(keyword=i["display"], source=i["source"], paper_count=len(i["papers"])) for i in items]


async def common_keywords(
    db: AsyncSession, pair: tuple[str, str], rows: list[Any], limit: int
) -> list[CommonKeyword]:
    papers, shown = _tally(rows)
    if len(rows) == 1:
        return _result(_pick([(k, "single_paper") for k in shown], papers, shown, limit, {}))

    repeated = sorted(
        (k for k in shown if len(papers[k]) >= _COMMON_KEYWORD_MIN_PAPERS), key=lambda k: -len(papers[k])
    )  # 안정 정렬이라 같은 편수면 최신 논문에서 먼저 나온 순
    candidates = [k for k in shown if len(papers[k]) < _COMMON_KEYWORD_MIN_PAPERS]
    order = [(k, "repeated") for k in repeated]
    filled = False
    if len(repeated) < limit and candidates:
        order += await _fill(db, pair, rows, repeated, candidates, shown)
        filled = True

    items = _pick(order, papers, shown, limit, {})
    if not _mixed_scripts(items):
        return _result(items)

    # 4단계. 상한(COMMON_KEYWORD_LIMIT_MAX)까지의 앞 순위를 물어, 합쳐서 빈 칸을 다음 순위로 채울 수 있게 한다.
    groups = await _same_meaning_groups(
        db, pair, rows, [shown[k] for k, _ in order[:COMMON_KEYWORD_LIMIT_MAX]]
    )
    items = _pick(order, papers, shown, limit, groups)
    if len(items) < limit and not filled and candidates:
        # 합치고 나니 겹치는 키워드만으로 칸이 모자라졌다 — 이제 3단계로 채운다.
        order += await _fill(db, pair, rows, repeated, candidates, shown)
        items = _pick(order, papers, shown, limit, groups)
    return _result(items)


async def get_coauthor_pair(
    db: AsyncSession, researcher_id: str, coauthor_id: str, *, keyword_limit: int
) -> CoauthorPairSummaryResponse:
    """논문 집합은 공저자 목록·함께 작성한 논문 목록과 같은 함수로 뽑아 건수가 어긋나지 않는다."""
    researcher = await get_profile(db, researcher_id)
    coauthor = await get_profile(db, coauthor_id)
    rows = await fetch_paper_rows(db, researcher_id, coauthor_id=coauthor_id)
    start, end = year_bounds(r.pubyear for r in rows)
    pair = tuple(sorted((researcher_id, coauthor_id)))
    return CoauthorPairSummaryResponse(
        researcher=_pair_person(researcher),
        coauthor=_pair_person(coauthor),
        co_paper_count=len(rows),
        period_start_year=start,
        period_end_year=end,
        common_keywords=await common_keywords(db, pair, rows, keyword_limit),
    )
