"""KCI 저자 번호로 연구자 번호 고르기 (scripts/judge_by_kci_author_ids.py)."""
from scripts.judge_by_kci_author_ids import pick_researcher_id


def test_믿을_논문에_가장_많이_나오는_번호가_본인이다():
    authors = {"a": {"ME", "X"}, "b": {"ME", "Y"}, "c": {"ME", "Z"}}
    assert pick_researcher_id(["a", "b", "c"], ["a", "b", "c"], authors) == ("ME", 1.0)


def test_믿을_논문이_1편이면_전체_논문_빈도로_동률을_가른다():
    # 믿을 논문 1편의 저자는 전원 1회로 동률 — 가르지 않으면 공저자 번호가 뽑힌다(곽이섭 오판정)
    authors = {"anchor": {"CO", "ME"}, "p1": {"ME"}, "p2": {"ME", "Q"}}
    cid, share = pick_researcher_id(["anchor"], ["anchor", "p1", "p2"], authors)
    assert cid == "ME" and share == 1.0


def test_조회한_논문이_없으면_판정하지_않는다():
    assert pick_researcher_id(["x"], ["x"], {}) == (None, 0.0)
