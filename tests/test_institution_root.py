"""기관명 루트 추출 규칙 회귀 테스트.

이 함수 값이 KCI articleSearch의 affiliation 필터로 그대로 들어가기 때문에,
잘못 자르면 그 연구자의 논문이 수집되지 않는다(실측: 이유진 1,426건 → 1건).
과거에 겪은 두 실패를 양쪽 다 고정해 둔다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.integrations.kci.researcher_client import (
    clean_keyword,
    institution_dept,
    institution_root,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 학과가 붙은 일반적인 형태
        ("청운대학교 식품영양학과", "청운대학교"),
        ("한양대학교 의과대학 신경과학교실", "한양대학교"),
        ("부경대학교 해양공학과", "부경대학교"),
        # 하위조직 이름이 또 다른 기관 접미사로 끝나는 경우 —
        # 최장일치로 뽑으면 통째로 남아 affiliation 필터가 논문을 못 찾는다.
        ("한서대학교 문화재보존과학연구센터", "한서대학교"),
        ("국방과학연구소 국방첨단과학기술연구원 Chem-Bio", "국방과학연구소"),
        ("광주여자대학교 일반대학원 치위생학과", "광주여자대학교"),
        # '대학'이 '대학교'보다 먼저 끝난다 — 최초종료로 뽑으면 잘린다.
        ("연세대학교", "연세대학교"),
        ("한국체육대학교", "한국체육대학교"),
        ("진주보건대학교 간호학과", "진주보건대학교"),
        # 접미사가 하나뿐이라 자를 곳이 없는 형태
        ("한국과학기술원", "한국과학기술원"),
        ("국립농업과학원 농업환경부 유기농업과", "국립농업과학원"),
        # 콤마로 여러 소속이 붙은 경우 앞의 것만 본다
        ("광주여자대학교 치위생학과, 광주여자대학교 일반대학원", "광주여자대학교"),
        # 접미사가 아예 없으면 첫 토큰
        ("㈜이오테크닉스", "㈜이오테크닉스"),
        (None, None),
        ("", None),
    ],
)
def test_institution_root(raw, expected):
    assert institution_root(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("청운대학교 식품영양학과", "식품영양학과"),
        ("한서대학교 문화재보존과학연구센터", "문화재보존과학연구센터"),
        ("연세대학교", None),
    ],
)
def test_institution_dept(raw, expected):
    assert institution_dept(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("항산화", "항산화"),
        ("  Antioxidant activity ", "Antioxidant activity"),
        ("2015 개정 교육과정", "2015 개정 교육과정"),
        # 점만 있는 값과 초록 본문이 통째로 들어온 값은 키워드가 아니다.
        ("....", None),
        ("Coreoperca herzi특징을 조사하였다. 재료 및 방법1. 채집 및 동정표본은 2001년 5월부터", None),
        ("", None),
        (None, None),
    ],
)
def test_clean_keyword(raw, expected):
    assert clean_keyword(raw) == expected
