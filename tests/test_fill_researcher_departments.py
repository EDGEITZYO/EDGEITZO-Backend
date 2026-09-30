"""논문 소속 문자열에서 전공 뽑기 (scripts/fill_researcher_departments.py)."""
import pytest

from scripts.fill_researcher_departments import department_from


@pytest.mark.parametrize("inst,expected", [
    ("충북대학교 환경공학과", "환경공학과"),
    ("농촌진흥청 국립농업과학원 토양비료과", "토양비료과"),  # 상위기관·구체 기관명은 뗀다
    ("OO대학교 심리학과 석사과정생", "심리학과"),         # 신분 표기는 뗀다
    ("국립농업과학원", None),                             # 기관명뿐이면 전공이 아니다
    ("한양대학교 대학원", None),                          # '대학원'은 전공이 아니다
])
def test_전공_추출(inst, expected):
    assert department_from(inst) == expected
