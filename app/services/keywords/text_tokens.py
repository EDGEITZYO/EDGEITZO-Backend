"""검색어 형태소 처리 공용 모듈.

Kiwi 인스턴스는 무겁고 상태가 없다. 모듈마다 새로 만들면 메모리만 배로 쓰므로
여기 하나만 두고 나눠 쓴다(검색 목적분류, 키워드맵 앵커 해석).
"""
from __future__ import annotations

from functools import lru_cache

from kiwipiepy import Kiwi

# 탐색 의도를 나타낼 뿐 연구 주제가 아닌 명사. 앵커 후보에서 뺀다 —
# "노화 관련 연구"에서 '연구'가 앵커가 되면 주제와 무관한 지도가 그려진다.
_SEARCH_STOPWORDS = frozenset({
    "연구", "논문", "관련", "분야", "자료", "내용", "결과", "최근", "요즘",
    "추천", "정보", "주제", "검색", "문헌", "학술", "저널",
})


@lru_cache(maxsize=1)
def get_kiwi() -> Kiwi:
    """지연 초기화 싱글턴. 첫 호출에서만 모델을 올린다."""
    return Kiwi()


def extract_nouns(text: str) -> list[str]:
    """검색어에서 주제 후보가 될 명사를 뽑는다.

    kiwi는 복합명사를 쪼갠다("조기진단" → "조기"+"진단"). 쪼개진 조각만 쓰면 의미가
    뭉개지므로, 원문에서 **붙어 있던** 명사는 도로 합친 형태도 후보에 넣는다.
    (불용어가 낀 덩어리는 합치지 않는다 — "노화관련" 같은 게 생긴다.)

    긴 것 먼저, 같은 길이면 원문에 먼저 나온 순서 — 구체적인 말을 먼저 시도하되
    순서가 입력에 대해 결정론적이어야 같은 질의가 늘 같은 앵커로 풀린다.
    """
    candidates: dict[str, int] = {}
    run: list = []  # 원문에서 공백 없이 이어진 명사 덩어리

    def _flush() -> None:
        if len(run) > 1 and not any(t.form in _SEARCH_STOPWORDS for t in run):
            compound = "".join(t.form for t in run)
            candidates.setdefault(compound, run[0].start)
        run.clear()

    for token in get_kiwi().tokenize(text):
        if token.tag not in ("NNG", "NNP"):
            _flush()
            continue
        if run and run[-1].start + run[-1].len != token.start:
            _flush()
        run.append(token)
        if len(token.form) >= 2 and token.form not in _SEARCH_STOPWORDS:
            candidates.setdefault(token.form, token.start)
    _flush()

    return sorted(candidates, key=lambda n: (-len(n), candidates[n]))
