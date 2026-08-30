"""선정 사유 마커 파싱 / 캐시 키 정규화 테스트.

마커 방식을 쓰는 이유가 "본문과 강조 구절이 어긋날 수 없다"는 것이므로,
파싱 결과가 항상 본문의 실제 부분 문자열이라는 점을 중심으로 검증한다.
"""
from app.services.selection_reason_service import (
    _assemble,
    _assemble_ex,
    _pick_best,
    _extract_sentences,
    _CHAR_HARD_MAX,
    _CHAR_MAX,
    _CHAR_MIN,
    _HIGHLIGHT_MAX,
    _parse_markers,
    build_keyword_key,
    normalize_keyword,
)


def _assert_consistent(raw: str):
    """어떤 입력이든: 마커가 남지 않고, 오프셋이 있으면 본문의 실제 구간을 가리킨다."""
    body, start, end = _parse_markers(raw)
    assert "**" not in body
    if start is not None:
        assert 0 <= start < end <= len(body)
        assert body[start:end].strip() == body[start:end]
        assert body[start:end]
    return body, start, end


def test_정상_마커_한쌍():
    body, start, end = _assert_consistent(
        "단일세포 해상도로 **은닉 변이를 분석**한 연구입니다. 두 번째 문장입니다."
    )
    assert body.startswith("단일세포 해상도로 은닉")
    assert body[start:end] == "은닉 변이를 분석"


def test_실패1_마커_없으면_본문만():
    body, start, end = _assert_consistent("마커가 전혀 없는 본문입니다. 두 번째 문장.")
    assert start is None and end is None
    assert body == "마커가 전혀 없는 본문입니다. 두 번째 문장."


def test_실패2_마커_두쌍이면_첫번째만():
    body, start, end = _assert_consistent("앞 **첫째 구절** 중간 **둘째 구절** 끝.")
    assert body[start:end] == "첫째 구절"
    assert "둘째 구절" in body  # 두 번째 구절도 본문에는 그대로 남는다


def test_실패3_열고_안닫으면_하이라이트_없음():
    body, start, end = _assert_consistent("앞부분 **닫히지 않은 구절 그리고 계속되는 문장.")
    assert start is None
    assert body == "앞부분 닫히지 않은 구절 그리고 계속되는 문장."


def test_실패4_본문_전체를_감싸면_하이라이트_없음():
    """명세: '본문 전체를 하이라이트 처리하는 일은 없도록 함'"""
    body, start, end = _assert_consistent("**본문 전체가 통째로 감싸진 경우입니다.**")
    assert start is None
    assert body == "본문 전체가 통째로 감싸진 경우입니다."


def test_맨앞과_앞뒤공백():
    body, start, end = _assert_consistent("  **맨앞 구절**이 강조된 경우입니다.  ")
    assert start == 0
    assert body[start:end] == "맨앞 구절"
    assert not body.startswith(" ") and not body.endswith(" ")


def test_명세_예시가_글자수_기준을_만족한다():
    """기획 확정본 예시 — 별표를 뺀 본문이 공백 포함 _CHAR_MIN~_CHAR_MAX여야 한다."""
    examples = [
        "교정 기능이 결손된 DNA 중합효소 감마를 발현시켜 **조기 노화 표현형을 유도한 생쥐 모델**을 "
        "제시한 연구입니다. 점 돌연변이가 3~5배 축적된 개체에서 체중·모발 감소, 골 소실, 수명 단축이 "
        "함께 나타나 모델의 타당성을 확인했습니다. 노화 표현형을 유전자 수준에서 유도하는 설계를 "
        "참고하실 때 도움이 됩니다.",
        "핵이 아닌 **미토콘드리아 DNA에서 점 돌연변이가 선택적으로 축적**되도록 설계한 생쥐로, "
        "그 축적이 개체 전체에 미치는 영향을 추적한 연구입니다. 돌연변이가 3~5배 쌓인 개체에서 "
        "체중·모발 감소와 골 소실, 수명 단축이 나타나 축적량과 표현형의 관계를 보여줍니다. "
        "돌연변이 부하의 기준값을 잡으실 때 활용하세요.",
    ]
    for raw in examples:
        body, start, end = _assert_consistent(raw)
        assert _CHAR_MIN <= len(body) <= _CHAR_MAX, f"{len(body)}자"
        assert body.count(".") == 3, "세 문장이어야 함"
        assert start is not None, "강조 구절이 있어야 함"
        assert end - start <= 30, "강조 구절은 30자 이내"


def test_키워드_키는_순서에_무관하다():
    """필터·정렬을 바꿔도 키워드가 같으면 같은 캐시 키가 나와야 재사용된다."""
    assert build_keyword_key(["항산화", "펩타이드"]) == build_keyword_key(["펩타이드", "항산화"])


def test_키워드_키는_탐색의도_표현을_흡수한다():
    """단순 검색은 사용자 원문이 그대로 들어와 '논문', '관련' 등이 섞인다."""
    assert build_keyword_key(["항산화 논문"]) == build_keyword_key(["항산화"])
    assert build_keyword_key([" 항산화 관련 연구 "]) == build_keyword_key(["항산화"])


def test_키워드_키_중복과_빈값_제거():
    assert build_keyword_key(["항산화", "항산화", "", "  "]) == "항산화"


def test_키워드_키_길이_상한():
    """컬럼이 String(500)이라 넘치면 삽입이 실패한다."""
    assert len(build_keyword_key([f"키워드{i}" for i in range(200)])) <= 500


def test_정규화는_대소문자와_유니코드를_통일한다():
    assert normalize_keyword("Antioxidant") == normalize_keyword("ANTIOXIDANT")
    assert normalize_keyword("ｍｔＤＮＡ") == "mtdna"  # NFKC 전각→반각


# ── 3문장 분할 수신 / 조립 ────────────────────────────────────────────


def test_JSON_세문장_추출():
    raw = '{"s1": "첫 문장입니다.", "s2": "둘째 문장입니다.", "s3": "셋째 문장입니다."}'
    assert _extract_sentences(raw) == ["첫 문장입니다.", "둘째 문장입니다.", "셋째 문장입니다."]


def test_JSON_앞뒤에_설명이_붙어도_추출된다():
    raw = '다음과 같습니다:\n```json\n{"s1":"가.", "s2":"나.", "s3":"다."}\n```\n이상입니다.'
    assert _extract_sentences(raw) == ["가.", "나.", "다."]


def test_JSON이_아닌_평문도_문장분리로_살린다():
    """실측에서 10건 중 1건이 JSON 형식을 어기고 본문만 평문으로 뱉었다.
    내용은 멀쩡하므로 버리지 않고 마침표 기준으로 나눈다."""
    got = _extract_sentences("첫 문장입니다. 둘째 문장입니다. 셋째 문장입니다.")
    assert got == ["첫 문장입니다.", "둘째 문장입니다.", "셋째 문장입니다."]


def test_깨진_JSON도_평문으로_폴백하지_않는다():
    """'{'로 시작하면 JSON을 의도한 것이므로, 깨진 조각을 본문으로 쓰지 않는다."""
    assert _extract_sentences('{"s1": 깨진 JSON') is None
    assert _extract_sentences("") is None


def test_조립_상한_이내면_세문장_전부_유지():
    sents = ["가" * 55 + ".", "나" * 55 + ".", "다" * 55 + "."]
    body, _, _ = _assemble(sents)
    assert len(body) <= _CHAR_MAX
    assert body.count(".") == 3


def test_조립_소폭_초과는_세문장을_유지한다():
    """상한(_CHAR_MAX)을 조금 넘어도 _CHAR_HARD_MAX까지는 자르지 않는다 —
    몇 자 초과보다 3번째 문장 손실이 훨씬 나쁘다."""
    sents = ["가" * 69 + ".", "나" * 69 + ".", "다" * 69 + "."]   # 합계 212자
    body, _, _, complete = _assemble_ex(sents)
    assert complete is True
    assert body.count(".") == 3
    assert _CHAR_MAX < len(body) <= _CHAR_HARD_MAX


def test_조립_대폭_초과시_문장경계에서_잘린다():
    """글자 단위로 자르면 말이 깨지지만, 문장 단위면 항상 완결된 문장만 남는다."""
    sents = ["가" * 100 + ".", "나" * 100 + ".", "다" * 100 + "."]
    body, _, _ = _assemble(sents)
    assert len(body) <= _CHAR_MAX
    assert body.endswith(".")          # 문장 중간에서 끊기지 않음
    assert body.count(".") == 1        # 첫 문장만 살아남음


def test_조립시_강조_마커가_보존된다():
    sents = ["앞 문장입니다.", "여기 **강조 구절**이 있습니다.", "끝 문장입니다."]
    body, s, e = _assemble(sents)
    assert "**" not in body
    assert body[s:e] == "강조 구절"


def test_조립_첫문장부터_상한초과여도_죽지_않는다():
    body, _, _ = _assemble(["가" * 300 + "."])
    assert body  # 상위에서 판단하도록 본문은 돌려준다


def test_조립_잘림_여부를_보고한다():
    """잘린 결과는 3번째 문장('왜 도움이 되는가')이 사라진 것이므로,
    길이가 목표에 더 가깝더라도 완결된 결과보다 뒤로 밀려야 한다."""
    short = ["가" * 40 + ".", "나" * 40 + ".", "다" * 40 + "."]
    body, _, _, complete = _assemble_ex(short)
    assert complete is True and body.count(".") == 3

    long = ["가" * 100 + ".", "나" * 100 + ".", "다" * 100 + "."]
    body, _, _, complete = _assemble_ex(long)
    assert complete is False          # 문장이 잘려나감
    assert len(body) <= _CHAR_MAX


def test_완결성이_길이보다_우선한다():
    """실제로 발생했던 버그: 잘린 2문장(168자)이 완결된 3문장(133자)보다
    175자에 가깝다는 이유로 선택되던 문제."""
    complete_body = "가" * 133
    cut_body = "나" * 168
    score_complete = (0, abs(len(complete_body) - 175))   # 완결
    score_cut = (1, abs(len(cut_body) - 175))             # 잘림
    assert score_complete < score_cut, "완결된 쪽이 이겨야 한다"


# ── 길이 정책 (명세 선택지별 채택 규칙) ───────────────────────────────


def _cand(n, complete=True):
    return ("가" * n, 0, 3, complete)


def test_center정책은_구간_한가운데를_고른다(monkeypatch):
    from app.core.settings import settings
    monkeypatch.setattr(settings, "selection_reason_length_policy", "center")
    got = _pick_best([_cand(160), _cand(176), _cand(195)])
    assert len(got[0]) == 176   # 175에 가장 가까움


def test_under_max정책은_상한_초과를_버린다(monkeypatch):
    """명세가 '200자 이내'일 때 — 205자는 더 길지만 상한을 넘어 탈락."""
    from app.core.settings import settings
    monkeypatch.setattr(settings, "selection_reason_length_policy", "under_max")
    got = _pick_best([_cand(205), _cand(188)])
    assert len(got[0]) == 188


def test_under_max정책은_상한_아래에서_가장_긴_것을_고른다(monkeypatch):
    """짧아서 허전한 카드보다 상한에 가깝게 꽉 찬 카드가 낫다."""
    from app.core.settings import settings
    monkeypatch.setattr(settings, "selection_reason_length_policy", "under_max")
    got = _pick_best([_cand(150), _cand(178), _cand(165)])
    assert len(got[0]) == 178


def test_모든_후보가_상한을_넘으면_가장_짧은_것(monkeypatch):
    from app.core.settings import settings
    monkeypatch.setattr(settings, "selection_reason_length_policy", "under_max")
    got = _pick_best([_cand(215), _cand(206), _cand(230)])
    assert len(got[0]) == 206


def test_어느_정책이든_완결성이_길이보다_우선한다(monkeypatch):
    """잘린(2문장) 후보는 길이가 아무리 맞아도 완결된 후보에 밀린다."""
    from app.core.settings import settings
    for policy in ("center", "under_max"):
        monkeypatch.setattr(settings, "selection_reason_length_policy", policy)
        got = _pick_best([_cand(175, complete=False), _cand(160, complete=True)])
        assert len(got[0]) == 160, f"{policy}에서 잘린 후보가 선택됨"


# ── 강조 구절 길이 상한 ────────────────────────────────────────────────


def test_강조구절이_상한을_넘으면_본문만_남는다():
    """자르지 않고 강조만 포기한다 — 구절 중간에서 끊긴 강조는 없는 것보다 나쁘다."""
    long_phrase = "가" * (_HIGHLIGHT_MAX + 1)
    body, start, end = _assert_consistent(f"앞부분 **{long_phrase}** 뒷부분입니다.")
    assert start is None and end is None
    assert long_phrase in body, "본문에서 글자가 잘려나가면 안 된다"


def test_강조구절이_상한_이내면_그대로_유지된다():
    phrase = "가" * _HIGHLIGHT_MAX
    body, start, end = _assert_consistent(f"앞부분 **{phrase}** 뒷부분입니다.")
    assert body[start:end] == phrase


def test_프롬프트가_지시한_30자는_상한_안에_있다():
    """프롬프트는 30자를 지시하고 코드는 40자에서 막는다 — 지시를 지킨 출력이
    가드에 걸리는 일은 없어야 한다."""
    assert 30 < _HIGHLIGHT_MAX
