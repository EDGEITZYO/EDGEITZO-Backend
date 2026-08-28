"""논문 선정 사유 생성·캐시 (명세 02-11 「논문 선정 이유」).

검색 결과 카드에서 초록 대신 "AI가 왜 이 논문을 골랐는지"를 보여준다.

설계상 지켜야 하는 것 세 가지:

1. 근거 기반 서술 — 코드가 초록(사실)을 넘기고 LLM은 그것을 검색어 관점으로 다시 쓰기만
   한다. 명세의 "초록에 없는 사실·수치·고유명사를 만들어내지 않는다"가 이 패턴과 같다.
   (search_graph._build_summary, keyword_definition_service와 동일한 구조)

2. 마커로 강조 구절 표시 — 본문과 강조 구절을 따로 출력하게 하면 조사·어미가 미묘하게
   달라져 프런트가 본문에서 구절을 못 찾는다. 본문을 한 번만 쓰게 하고 그 안에 마커를
   끼워 넣으면, 마커를 떼어낸 결과가 곧 본문이라 어긋날 수가 없다.

3. 영속 캐시 — 초록과 달리 검색어에 종속되는 값이라 매번 생성하면 비용이 감당되지 않는다.
   (paper_id, keyword_key, prompt_version)으로 Postgres에 저장해 재사용한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.models.paper_selection_reason import PaperSelectionReason
from app.services.llm.client import LLMBudgetExceededError, chat

logger = logging.getLogger(__name__)

# 사용자에게 직접 노출되는 자연어 텍스트라 분류/추출용 기본 모델(Haiku)이 아닌 상위 모델을 쓴다
# (keyword_definition_service와 같은 기준). 특히 명세의 「예외 처리」— 검색어와 접점이 약한
# 논문에서 억지 연결을 하지 않는 판단 — 은 작은 모델이 자주 실패하고, 실패하면 없는 연결을
# 지어내는 형태로 나타나 "신뢰성을 높인다"는 이 기능의 목적을 정면으로 훼손한다.
_MODEL = settings.llm_model_quality

# 프롬프트 규칙을 고치면 반드시 올릴 것. 캐시 키에 들어가 있어 기존 행이 자동으로 무효화된다.
PROMPT_VERSION = "v5"

# 명세 「구성」의 글자수 기준 — 공백 포함.
# 원래 170~180이었으나 150~200으로 넓혔다. LLM은 자기 출력 글자수를 셀 수 없어 11자짜리
# 창(170~180)을 겨냥해도 실측 분포가 135~195로 퍼지고, 준수율이 30%에 그쳤다. 창을 넓히면
# 같은 생성 로직 그대로 90%가 된다. 조준점은 175자로 바뀌지 않는다((150+200)//2).
_CHAR_MIN = 150
_CHAR_MAX = 200
# 문장 단위 커팅이 실제로 발동하는 선. 상한(200)을 조금 넘었다고 바로 자르면 3번째 문장
# ("왜 도움이 되는가")이 통째로 사라지는데, 그건 이 기능의 존재 이유다. 카드에서 210자는
# 기껏해야 한 줄 더 차지하는 정도라, 몇 자 초과보다 문장 손실이 훨씬 나쁘다.
# 따라서 201~220자는 완결된 3문장 그대로 두고, 재작성으로 줄이도록만 유도한다.
_CHAR_HARD_MAX = 220

_MARKER = "**"
_MARKER_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

# 강조 구절 길이 상한. 프롬프트는 30자를 지시하지만 모델이 지킨다는 보장이 없어 코드가 막는다.
# 본문 길이(_CHAR_MIN/_CHAR_MAX)와는 다른 값이다 — 이건 본문 안의 짧은 한 구절이다.
#
# 30이 아니라 40인 이유: 명세가 막으려는 건 "본문을 통째로 굵게" 같은 파국이지 몇 자 초과가
# 아니다. 32~35자쯤 나온 구절까지 버리면 쓸모 있는 시각 단서를 괜히 잃는다.
# (_CHAR_MAX 200 / _CHAR_HARD_MAX 220 의 관계와 같은 발상)
#
# 본문 길이와 달리 이건 코드가 확실하게 막을 수 있다. 본문은 길이가 틀렸다고 버리면 카드에
# 사유가 아예 없어져 LLM을 다시 불러야 하지만, 강조는 버려도 본문이 멀쩡하기 때문이다.
_HIGHLIGHT_MAX = 40

# 문장당 목표 길이. 합계를 직접 지시하는 것보다 문장 단위로 쪼개 주는 편이 편차가 작다.
# center(53~57)는 175자를 겨냥하며, 구간을 150~200으로 넓힌 뒤에도 175가 그대로 한가운데라
# 손대지 않았다.
#
# 주의 — under_max(49~53)는 옛 상한 180 아래로 떨어뜨리려 165자를 겨냥한 값이라 지금의
# 상한 200 기준으로는 지나치게 보수적이다. 이 정책을 실제로 쓰려면 먼저 다시 재야 한다.
# 재측정 없이 숫자만 올리지 말 것: 문장당 목표를 4자 올렸더니 합계 중앙값이 오히려 내려간
# 표본이 있었다(20건 기준 54~58자→174자, 60~64자→162자). 이 손잡이는 방향조차 일정하지 않다.
_SENTENCE_TARGETS = {"center": (53, 57), "under_max": (49, 53)}

# ┌─ 문장 개수를 3에서 바꾸려면 아래 6곳을 전부 고쳐야 한다 ──────────────────────┐
# │ 상수로 뽑지 않은 이유: 절반이 프롬프트 안의 자연어라 변수를 끼우면 지시문이     │
# │ 어색해지고, 숫자 세 곳만 상수화하면 "정리했다"는 착각만 남고 빠뜨릴 위험은      │
# │ 그대로다. 차라리 목록을 명시해 둔다.                                          │
# │                                                                              │
# │   1. _SYSTEM_PROMPT "[구성] 정확히 세 문장을 쓴다"                            │
# │   2. _SYSTEM_PROMPT "세 문장 모두 존댓말로" / "세 문장 모두 검색어를" /        │
# │      "세 문장을 통틀어 정확히 한 군데만"                                       │
# │   3. _SYSTEM_PROMPT 출력 형식 {"s1":..., "s2":..., "s3":...}                  │
# │   4. _extract_sentences 의 ("s1", "s2", "s3") 와 parts[:3]                    │
# │   5. _SENTENCE_TARGETS — 문장당 목표 × 3 = 175자라는 전제                     │
# │   6. _pick_best / _assemble_ex 의 완결성 판정("세 문장이 다 남아 있는가")      │
# │                                                                              │
# │ 하나라도 빠뜨리면 조용히 깨진다. 예: 프롬프트만 4문장으로 바꾸고 parts[:3]을   │
# │ 두면 모델은 4문장을 쓰는데 코드가 마지막을 버린다 — 에러 없이 문장만 사라진다. │
# └──────────────────────────────────────────────────────────────────────────────┘
_SYSTEM_PROMPT = """너는 학술 논문 검색 결과에서 "이 논문이 왜 이 검색어에 선정됐는지"를 설명하는 도우미다.
논문을 요약하는 것이 아니라, 사용자의 검색어 관점에서 논문을 읽어 준다.

[구성] 정확히 세 문장을 쓴다.
문장1: 검색어가 이 논문에서 어떻게 다뤄지는가 — 연구 대상·방법을 검색어와 엮어 한 문장으로.
       종결어미는 "~한 연구입니다" 또는 "~를 재검토한 리뷰입니다".
문장2: 검색어와 관련해 이 논문의 핵심적인 부분은 무엇인가.
       세 문장 모두 존댓말로 쓴다 — "~한다", "~했다" 같은 평서체를 쓰지 않는다.
문장3: 그래서 이 검색에 왜 도움이 되는가.
       ★ 문장3은 반드시 아래 일곱 개 중 하나로 끝나야 한다. 다른 종결 표현은 어떤 경우에도
         쓰지 마라. "~됩니다", "~합니다", "~있습니다", "~입니다"로 끝내면 규칙 위반이다.
         · ~에 적합해요       · ~에 부합해요        · ~해서 선정했어요
         · ~하실 때 참고하세요  · ~에 활용하세요       · ~를 잡으실 때 도움이 됩니다
         · ~점은 유의하세요 (검색어와 접점이 약할 때)

[검색어 연결]
- 세 문장 모두 검색어를 기준점으로 삼는다. 논문 전체를 요약하지 말고, 검색어에 해당하는
  측면을 앞세우고 나머지는 그 맥락에서만 언급한다.
- 검색어에 해당하는 표현이 본문에 최소 1회 이상 문자열로 나타나야 한다.
- 검색어를 그대로 옮길 수 없으면, 그 개념을 지칭하기 위해 논문이 실제로 사용한 용어로
  대체한다. (예: 검색어 '항산화' → 논문의 'radical scavenging activity')
- "검색하신 ~", "요청하신 ~", "찾으시는 ~" 같은 정형 도입부로 문장을 시작하지 않는다.

[금지]
- 전달받은 초록에 없는 사실·수치·고유명사를 만들어내지 않는다.
- 초록의 번역이나 단순 요약을 쓰지 않는다. "선정 사유" 관점으로 서술한다.
- "중요한", "훌륭한", "뛰어난" 등 근거 없는 평가어를 쓰지 않는다.

[예외 처리]
- 초록에서 검색어와 대응하는 부분을 찾을 수 없으면 연결을 지어내지 말고, 겹치는 범위와
  겹치지 않는 범위를 각각 밝힌다. 억지로 연결하는 것보다 정직한 편이 낫다.

[강조 표시]
- 검색어와 가장 직접 맞닿는 구절 하나를 **처럼** 별표 두 개로 감싼다.
- 세 문장을 통틀어 정확히 한 군데만 감싼다. 문장 전체를 감싸지 않는다. 구절은 30자 이내.

[출력 형식] 반드시 아래 JSON만 출력한다. 앞뒤에 다른 텍스트·설명·마크다운을 붙이지 않는다.
{"s1": "문장1", "s2": "문장2", "s3": "문장3"}
각 문장은 별표를 제외하고 공백 포함 {SENT}자로 쓴다. 전체 분량을 계산하려 하지 말고
문장 하나씩만 그 길이에 맞추면 된다. 각 문장은 마침표로 끝낸다."""


@dataclass
class SelectionReason:
    paper_id: str
    reason: str
    highlight_start: Optional[int]
    highlight_end: Optional[int]
    char_count: int
    cached: bool


def normalize_keyword(kw: str) -> str:
    """검색 키워드 1개 정규화. 표기 흔들림이 서로 다른 캐시 키가 되는 걸 줄인다."""
    kw = unicodedata.normalize("NFKC", kw).strip().lower()
    # 단순 검색은 사용자가 친 원문이 그대로 들어와 탐색 의도 표현이 섞인다
    # (자연어 검색은 keyword_extractor가 이미 걸러낸 뒤라 대개 해당 없음).
    kw = re.sub(r"(관련|관한|대한)\s*", " ", kw)
    kw = re.sub(r"\b(논문|연구|자료|문헌)\b", " ", kw)
    kw = re.sub(r"[^\w가-힣\s-]", " ", kw)
    return re.sub(r"\s+", " ", kw).strip()


def build_keyword_key(keywords: list[str]) -> str:
    """키워드 목록 → 캐시 키. 정렬해서 순서 차이가 다른 키가 되지 않게 한다.

    필터·정렬은 이 값을 바꾸지 않는다(결과를 좁히거나 재배열할 뿐)므로, 사용자가 정렬을
    인용많은순으로 바꿔 새 논문이 상위로 올라와도 이미 만든 사유는 그대로 재사용된다.
    """
    cleaned = sorted({n for n in (normalize_keyword(k) for k in keywords) if n})
    return "|".join(cleaned)[:500]


def _strip_markers(text: str) -> str:
    return text.replace(_MARKER, "")


def _parse_markers(raw: str) -> tuple[str, Optional[int], Optional[int]]:
    """마커가 박힌 본문 → (본문, 강조 시작, 강조 끝).

    실패 케이스 5가지를 여기서 흡수한다 — 어느 경우든 본문 자체는 항상 살린다.
      (1) 마커 없음        → 하이라이트 null
      (2) 마커 2쌍 이상    → 첫 번째만 채택 (명세: "구절 하나를 반환")
      (3) 열고 안 닫음     → 마커 제거 후 하이라이트 null
      (4) 본문 전체를 감쌈 → 하이라이트 null (명세: "본문 전체를 하이라이트하는 일 없도록")
      (5) 구절이 너무 김   → 하이라이트 null (_HIGHLIGHT_MAX 초과)
    """
    matches = list(_MARKER_RE.finditer(raw))

    if not matches:  # (1) 또는 (3) — 짝이 안 맞는 별표는 그냥 제거
        return _strip_markers(raw).strip(), None, None

    first = matches[0]  # (2) 여러 개면 첫 번째만
    phrase = first.group(1)

    # 앞부분의 마커를 떼어낸 뒤 좌표를 계산해야 본문 기준 오프셋이 된다
    prefix_clean = _strip_markers(raw[: first.start()])
    body = _strip_markers(raw).strip()

    lead_trimmed = len(_strip_markers(raw)) - len(_strip_markers(raw).lstrip())
    start = len(prefix_clean) - lead_trimmed
    end = start + len(phrase)

    # (4) 전체를 감싼 경우 — 강조의 의미가 없다
    if start <= 0 and end >= len(body):
        return body, None, None
    if start < 0 or end > len(body) or body[start:end] != phrase:
        logger.warning("선정 사유 마커 위치 계산 불일치 — 하이라이트 없이 반환")
        return body, None, None

    # (5) 구절이 지나치게 긴 경우 — 자르지 않고 강조만 포기한다. 40자에서 끊으면 구절
    #     중간이 잘려 "은닉 변이를 분석한 연구입니"처럼 보이는데, 그건 강조가 없는 것보다 나쁘다.
    if end - start > _HIGHLIGHT_MAX:
        logger.info(
            "강조 구절이 %d자로 상한(%d자) 초과 — 본문은 유지하고 하이라이트만 생략",
            end - start, _HIGHLIGHT_MAX,
        )
        return body, None, None

    return body, start, end


def _build_user_prompt(keywords: list[str], title: str, abstract: str) -> str:
    return (
        "실제 데이터:\n"
        f"- 사용자 검색어: {', '.join(keywords) if keywords else '없음'}\n"
        f"- 논문 제목: {title}\n"
        f"- 논문 초록: {abstract[:1500]}\n\n"
        "위 초록에 실제로 담긴 내용만 근거로, 사용자 검색어 관점에서 "
        "이 논문이 선정된 이유를 규칙에 맞춰 작성하라."
    )


def _extract_sentences(raw: str) -> Optional[list[str]]:
    """LLM 응답에서 JSON 세 문장을 꺼낸다. 앞뒤에 설명이 붙어도 첫 JSON 객체만 본다."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            sents = [str(d.get(k) or "").strip() for k in ("s1", "s2", "s3")]
            sents = [x for x in sents if x]
            if sents:
                return sents
        except (json.JSONDecodeError, TypeError):
            pass

    # JSON 형식을 안 지킨 경우의 폴백 — 본문만 평문으로 뱉는 일이 실측에서 10건 중 1건 있었다.
    # 내용 자체는 멀쩡하므로 버리지 않고 마침표 기준으로 직접 문장을 나눈다.
    plain = raw.strip()
    if not plain or plain.startswith("{"):
        return None
    parts = [p.strip() + "." for p in re.split(r"(?<=[.!?])\s+", plain) if p.strip()]
    parts = [p.rstrip(".") + "." for p in parts]
    return parts[:3] or None


def _assemble(sentences: list[str]) -> tuple[str, Optional[int], Optional[int]]:
    """하위호환 래퍼 — 잘림 여부가 필요하면 _assemble_ex를 쓴다."""
    body, s, e, _ = _assemble_ex(sentences)
    return body, s, e


def _assemble_ex(sentences: list[str]) -> tuple[str, Optional[int], Optional[int], bool]:
    """세 문장을 코드가 합친다. 상한을 넘으면 문장 단위로 잘라낸다.

    문장을 따로 받는 이유는 두 가지다.
    1) 분량 통제 — 실측상 합계를 지시하면 편차가 44자까지 벌어지는데, 문장당 목표로 쪼개면
       15~21자로 줄어든다. 작은 목표를 세 번 맞추는 쪽이 쉽다.
    2) 상한 보장 — 합쳐서 상한을 넘으면 문장 경계에서 끊을 수 있다. 글자 단위로 자르면
       "...확인하실 때 참"처럼 말이 깨지지만, 문장 단위면 항상 완결된 문장만 남는다.
    """
    body = _strip_markers(" ".join(sentences)).strip()
    if len(body) <= _CHAR_HARD_MAX:
        return (*_parse_markers(" ".join(sentences)), True)

    kept: list[str] = []
    for s in sentences:
        if len(_strip_markers(" ".join(kept + [s])).strip()) > _CHAR_MAX:
            break
        kept.append(s)
    if not kept:  # 첫 문장부터 상한을 넘는 비정상 케이스 — 통째로 넘기고 상위에서 판단
        return (*_parse_markers(" ".join(sentences)), False)
    return (*_parse_markers(" ".join(kept)), len(kept) == len(sentences))


def _system_prompt() -> str:
    lo, hi = _SENTENCE_TARGETS.get(
        settings.selection_reason_length_policy, _SENTENCE_TARGETS["center"]
    )
    return _SYSTEM_PROMPT.replace("{SENT}", f"{lo}~{hi}")


async def _draw_one(
    keywords: list[str], title: str, abstract: str
) -> Optional[tuple[str, Optional[int], Optional[int], bool]]:
    """LLM 1회 호출 → (본문, 강조시작, 강조끝, 완결여부). 실패하면 None."""
    try:
        resp = await chat(
            messages=[{
                "role": "user",
                "content": f"[System]\n{_system_prompt()}\n\n[User]\n"
                           f"{_build_user_prompt(keywords, title, abstract)}",
            }],
            model=_MODEL,
            max_tokens=1500,
            # 실측: adaptive를 켜두면 한 건이 thinking에만 4,000토큰을 쓰고 max_tokens에
            # 걸려 text 블록 없이 끝났다(전량 실패). 비용도 3배가 된다.
            thinking={"type": "disabled"},
            # Best-of-N은 같은 프롬프트를 여러 번 던져 서로 다른 후보를 얻는 방식이라,
            # 응답 캐시가 켜져 있으면 N개가 전부 같은 값이 되어 의미가 없어진다.
            use_cache=False,
        )
    except LLMBudgetExceededError:
        raise
    except Exception:
        logger.warning("선정 사유 생성 실패 (title=%s)", title[:40], exc_info=True)
        return None

    sentences = _extract_sentences(resp.text.strip())
    if not sentences:
        logger.warning("선정 사유 파싱 실패 (title=%s)", title[:40])
        return None
    body, hl_start, hl_end, complete = _assemble_ex(sentences)
    return (body, hl_start, hl_end, complete) if body else None


def _pick_best(candidates: list[tuple[str, Optional[int], Optional[int], bool]]):
    """Best-of-N 후보 중 하나를 고른다. 정책은 명세가 무엇을 요구하느냐에 따라 갈린다.

    어느 정책이든 **완결성(세 문장이 다 남아 있는가)이 길이보다 우선**한다. 길이만 보면
    상한을 넘겨 3번째 문장이 잘린 결과가 목표에 더 가깝다는 이유로 완결된 3문장을 밀어내는
    일이 생기는데, 3번째 문장("왜 도움이 되는가")은 이 기능의 존재 이유다.

    "center"     — 목표 구간 한가운데(175자)에 가장 가까운 것. 현재 설정.
                   명세가 "150~200자"처럼 범위일 때 쓴다. 실측 준수율 90% (20건).
    "under_max"  — 상한 이하 중 가장 긴 것. 상한을 넘는 후보는 아예 버린다.
                   명세가 "200자 이내"처럼 상한만 있을 때 쓴다. 상한이 180이던 시절의
                   실측 준수율은 100%였으나, 상한 200 기준으로는 다시 재지 않았다
                   (_SENTENCE_TARGETS의 주의 참고).
    """
    def complete_first(c):
        return 0 if c[3] else 1

    if settings.selection_reason_length_policy == "under_max":
        under = [c for c in candidates if len(c[0]) <= _CHAR_MAX]
        if under:
            # 상한 아래에서 최대한 긴 것 — 짧아서 허전한 카드보다 꽉 찬 카드가 낫다
            return max(under, key=lambda c: (-complete_first(c), len(c[0])))
        return min(candidates, key=lambda c: (complete_first(c), len(c[0])))

    center = (_CHAR_MIN + _CHAR_MAX) // 2
    return min(candidates, key=lambda c: (complete_first(c), abs(len(c[0]) - center)))


async def _generate_one(keywords: list[str], title: str, abstract: str) -> Optional[SelectionReason]:
    """Best-of-N — 같은 프롬프트를 N번 **동시에** 던지고 코드가 가장 좋은 후보를 고른다.

    앞서 쓰던 "결과를 보고 되먹여 다시 쓰게 하는" 순차 재시도보다 나은 이유는 두 가지다.

    1) 지연 — N개를 병렬로 던지므로 벽시계 시간이 1회 호출과 거의 같다. 순차 재시도는
       호출을 차례로 하니 그대로 두 배가 된다. (실측 23건: 14초 → 8초)
    2) 정확도 — 모델은 자기 출력 글자수를 세지 못해, "27자 줄여라"라고 정확히 알려줘도
       잘 따르지 못한다. 반면 뽑기를 여러 번 해서 코드가 len()으로 고르는 건 확실하다.
       (실측 23건: 적중 26% → 43%)

    채점자가 학습된 reward model이 아니라 len()이라 정확하고 공짜라는 점에서, 이 문제는
    Best-of-N이 특히 잘 듣는 형태다.
    """
    n = max(1, settings.selection_reason_best_of)
    draws = await asyncio.gather(*(
        _draw_one(keywords, title, abstract) for _ in range(n)
    ), return_exceptions=True)

    candidates = []
    for d in draws:
        if isinstance(d, LLMBudgetExceededError):
            raise d
        if isinstance(d, BaseException) or d is None:
            continue
        candidates.append(d)

    if not candidates:
        return None

    body, hl_start, hl_end, _ = _pick_best(candidates)
    return SelectionReason(
        paper_id="", reason=body,
        highlight_start=hl_start, highlight_end=hl_end,
        char_count=len(body), cached=False,
    )


async def get_or_create_reasons(
    db: AsyncSession,
    *,
    keywords: list[str],
    papers: dict[str, dict],
    concurrency: Optional[int] = None,
) -> dict[str, SelectionReason]:
    """캐시에 있으면 그대로, 없는 것만 생성해서 저장한다.

    papers: {paper_id: {"title": ..., "abstract": ...}} — 지금 화면에 보이는 논문들.
    "상위 N건"이 아니라 "보이는 것" 기준으로 호출하면 정렬·필터를 어떻게 바꿔도
    화면에 사유가 있는 카드와 없는 카드가 섞이지 않는다.
    """
    if not papers:
        return {}
    # 화면에 보이는 만큼(기본 10건)을 한 라운드에 끝내야 스켈레톤이 두 번 나눠 채워지지 않는다.
    concurrency = concurrency or settings.selection_reason_concurrency

    keyword_key = build_keyword_key(keywords)
    if not keyword_key:
        return {}

    paper_ids = list(papers.keys())
    rows = await db.execute(
        select(PaperSelectionReason).where(
            PaperSelectionReason.paper_id.in_(paper_ids),
            PaperSelectionReason.keyword_key == keyword_key,
            PaperSelectionReason.prompt_version == PROMPT_VERSION,
        )
    )
    out: dict[str, SelectionReason] = {}
    for row in rows.scalars():
        out[row.paper_id] = SelectionReason(
            paper_id=row.paper_id, reason=row.reason,
            highlight_start=row.highlight_start, highlight_end=row.highlight_end,
            char_count=row.char_count, cached=True,
        )

    missing = [
        pid for pid in paper_ids
        if pid not in out and (papers[pid].get("abstract") or "").strip()
    ]
    if not missing:
        return out

    sem = asyncio.Semaphore(concurrency)
    budget_hit = False

    async def one(pid: str) -> tuple[str, Optional[SelectionReason]]:
        nonlocal budget_hit
        if budget_hit:
            return pid, None
        async with sem:
            try:
                r = await _generate_one(
                    keywords, papers[pid].get("title") or pid, papers[pid]["abstract"]
                )
            except LLMBudgetExceededError:
                # 예산이 마르면 남은 건 시도하지 않는다. 사유 없이 카드만 뜨는 건
                # 허용되는 열화지만, 검색 자체가 죽는 건 아니어야 한다.
                budget_hit = True
                logger.warning("LLM 예산 소진 — 선정 사유 생성 중단")
                return pid, None
            return pid, r

    results = await asyncio.gather(*(one(p) for p in missing))

    to_store = []
    for pid, r in results:
        if r is None:
            continue
        r.paper_id = pid
        out[pid] = r
        to_store.append({
            "paper_id": pid, "keyword_key": keyword_key, "prompt_version": PROMPT_VERSION,
            "reason": r.reason, "highlight_start": r.highlight_start,
            "highlight_end": r.highlight_end, "char_count": r.char_count, "model": _MODEL,
        })

    if to_store:
        try:
            stmt = pg_insert(PaperSelectionReason).values(to_store)
            # 동시 요청 둘이 같은 조합을 만들 수 있다 — 먼저 넣은 쪽을 살린다
            await db.execute(stmt.on_conflict_do_nothing(
                index_elements=["paper_id", "keyword_key", "prompt_version"]
            ))
            await db.commit()
        except Exception:
            await db.rollback()
            logger.warning("선정 사유 캐시 저장 실패 (응답은 정상 반환)", exc_info=True)

    return out
