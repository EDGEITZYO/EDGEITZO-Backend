"""소속 대조 (scripts/load_researchers._inst_match). 적재·정리 스크립트가 같이 쓴다."""
import pytest

from scripts.load_researchers import _inst_match


@pytest.mark.parametrize("a,b", [
    ("서울대학교", "서울대학교병원"),
    ("국립식량과학원", "농촌진흥청 국립식량과학원 중부작물부"),
    ("공주대학교", "국립공주대학교"),
    ("농업과학기술원", "농촌진흥청농업과학기술원"),
    ("Sunmoon University", "Department of Physics, Sunmoon University, Asan"),
    ("연세대학교", "연세대학교"),
])
def test_같은_기관(a, b):
    assert _inst_match(a, b) and _inst_match(b, a)


@pytest.mark.parametrize("a,b", [
    ("서울대학교", "남서울대학교"),  # 부분 문자열이던 시절 한 사람으로 합쳐졌다
    ("부산대학교", "동부산대학교"),
    ("연세대학교", "고려대학교"),
    ("연세대학교", ""),
])
def test_다른_기관(a, b):
    assert not _inst_match(a, b)


@pytest.mark.parametrize("a,b", [
    ("(주)한화 종합연구소", "(주)한화 종합연구소 레이저개발2팀"),
    ("(재)전남생물산업진흥원", "(재)전남생물산업진흥원 천연자원연구센터"),
    ("지오메디칼", "(주) 지오메디칼"),
])
def test_법인_표기가_있어도_같은_기관(a, b):
    assert _inst_match(a, b)


def test_법인_표기만_같으면_다른_기관():
    assert not _inst_match("(주) 지오메디칼", "(주) 그린케미칼")
