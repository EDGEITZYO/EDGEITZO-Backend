"""연구자 논문 판정 규칙 (scripts/reconcile_researcher_papers.py). KCI·DB 없이 규칙만 본다."""
from scripts.reconcile_researcher_papers import institution_units, judge, other_person_evidence, specific_unit


def paper(own_inst, coauthors, name="김민정"):
    authors = [name, *coauthors]
    return {"authors": authors, "author_insts": [own_inst] + ["기관"] * len(coauthors)}


class TestInstitutionUnits:
    def test_상위기관으로_시작하면_하위기관도_단위로_쓴다(self):
        # institution_root만 쓰면 '농촌진흥청'만 남아 '국립농업과학원'으로 적힌 논문을 놓쳤다
        assert institution_units("농촌진흥청 국립농업과학원 유기농업과") == ["농촌진흥청", "국립농업과학원"]

    def test_대학_하위조직은_단위로_쓰지_않는다(self):
        # '의과대학'으로 검색하면 전국 의대가 걸린다
        assert institution_units("연세대학교 의과대학") == ["연세대학교"]

    def test_소속이_없으면_빈_목록(self):
        assert institution_units(None) == []


class TestJudge:
    def test_소속이_맞으면_본인_논문(self):
        v = judge("김민정", ["국립농업과학원"], {"a": paper("국립농업과학원 유기농업과", ["박", "최"])})
        assert v == {"a": "inst"}

    def test_소속이_달라도_확정_논문과_공저자가_2명_겹치면_본인_논문(self):
        v = judge("김민정", ["국립농업과학원"], {
            "core": paper("국립농업과학원", ["박지훈", "최서현", "이도윤"]),
            "moved": paper("서울대학교", ["박지훈", "최서현"]),
            "one": paper("서울대학교", ["박지훈", "강하늘"]),
        })
        assert v["moved"] == "coauthor"
        assert v["one"] == "reject"  # 1명만 겹치면 근거가 부족하다

    def test_공저자로_확인된_소속이_2편_이상이면_그_소속을_인정한다(self):
        # 기관 개칭 후 팀이 바뀐 시기 — 공저자는 안 겹치지만 소속이 같은 기관의 새 이름
        v = judge("이원석", ["국립환경연구원"], {
            "old": paper("국립환경연구원", ["김보경", "김지인"], "이원석"),
            "bridge1": paper("국립환경과학원", ["김보경", "김지인"], "이원석"),
            "bridge2": paper("국립환경과학원", ["김보경", "김지인", "남용재"], "이원석"),
            "later": paper("국립환경과학원", ["정동환", "정현미"], "이원석"),
        })
        assert v["later"] == "learned_inst"

    def test_확인된_소속이_1편뿐이면_인정하지_않는다(self):
        v = judge("이원석", ["국립환경연구원"], {
            "old": paper("국립환경연구원", ["김보경", "김지인"], "이원석"),
            "bridge": paper("국립환경과학원", ["김보경", "김지인"], "이원석"),
            "later": paper("국립환경과학원", ["정동환", "정현미"], "이원석"),
        })
        assert v["later"] == "reject"

    def test_다른_기관의_동명이인은_뺀다(self):
        v = judge("김미경", ["연세대학교"], {
            "mine": paper("연세대학교", ["허준", "유수홍"], "김미경"),
            "other": paper("계명대학교", ["배재현", "최종한"], "김미경"),
        })
        assert v["other"] == "reject"

    def test_저자_정보가_없으면_판정하지_않는다(self):
        assert judge("김민정", ["국립농업과학원"], {"x": {"authors": []}}) == {"x": "unknown"}


class TestSpecificUnit:
    def test_상위기관_뒤의_구체_기관을_쓴다(self):
        assert specific_unit("농촌진흥청 국립식량과학원 중부작물부") == "국립식량과학원"

    def test_상위기관만_있으면_없음(self):
        # 산하 연구원 중 어디인지 모른다 — 이걸로 대조하면 다른 연구원의 동명이인이 들어온다
        assert specific_unit("농촌진흥청") is None

    def test_대학은_대학_단위(self):
        assert specific_unit("연세대학교 의과대학") == "연세대학교"


class TestParentAgencyOnly:
    def test_상위기관만_적힌_논문은_소속_근거가_되지_않는다(self):
        v = judge("김현주", ["국립식량과학원"], {
            "mine": paper("농촌진흥청 국립식량과학원", ["박", "최"], "김현주"),
            "parent_only": paper("농촌진흥청 작물과학원", ["정", "한"], "김현주"),
        })
        assert v["parent_only"] == "reject"

    def test_띄어쓰기_없이_붙은_옛_기관명도_나눈다(self):
        # 국립농업과학원의 2008년 이전 이름. 나누지 않으면 '농촌진흥청'만 남아 확인된 소속이 못 된다
        assert specific_unit("농촌진흥청농업과학기술원") == "농업과학기술원"

    def test_붙은_표기는_확인된_상위기관에만_적용한다(self):
        assert specific_unit("동부대학교") == "동부대학교"


class TestMergedAuthorCell:
    def test_한_칸에_붙은_저자에서도_본인을_찾는다(self):
        v = judge("박윤수", ["성균관대학교"], {
            "a": {"authors": ["박윤수,문영완,임지순"], "author_insts": ["성균관대학교"]},
        })
        assert v == {"a": "inst"}


class TestCorpMarkers:
    def test_법인_표기는_단위가_되지_않는다(self):
        assert specific_unit("(주) 지오메디칼") == "지오메디칼"
        assert specific_unit("(재)전남생물산업진흥원 천연자원연구센터") == "전남생물산업진흥원"


class TestOtherPersonEvidence:
    def test_다른_구체_기관이면_지울_근거(self):
        assert other_person_evidence("김미경", ["연세대학교"], paper("계명대학교", ["배"], "김미경"))

    def test_영문_소속은_비교할_수_없어_근거가_아니다(self):
        assert not other_person_evidence("홍혜현", ["선문대학교"], paper("Sunmoon University", ["박"], "홍혜현"))

    def test_상위기관만_적혔으면_근거가_아니다(self):
        assert not other_person_evidence("조명래", ["국립원예특작과학원"], paper("농촌진흥청", ["박"], "조명래"))

    def test_빈_소속은_근거가_아니다(self):
        assert not other_person_evidence("정인숙", ["부산대학교"], paper("", ["박"], "정인숙"))

    def test_깨진_소속_칸에서도_같은_기관을_알아본다(self):
        assert not other_person_evidence(
            "정인숙", ["부산대학교"], paper("Jeong, Ihn Sook)(부산대학교 간호대학", ["박"], "정인숙"))
