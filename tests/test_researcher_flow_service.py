import numpy as np
import pytest
from types import SimpleNamespace

from app.schemas.researcher_detail import ResearchFlowCluster, ResearchFlowClusterPaper
from app.services import researcher_flow_service as flow


def row(ext, year, month="01", keywords=None, internal=None, title="제목"):
    return SimpleNamespace(
        external_id=ext, internal_paper_id=internal, title=title, pubyear=year,
        pubmonth=month, keywords=keywords or [], authors=[], url=None, citation_count=0,
    )


class TestPaperSignature:
    """캐시 무효화 키. 여기가 틀리면 편입이 끝나도 옛 응답이 계속 나간다."""

    def test_논문이_늘면_서명이_바뀐다(self):
        before = [row("A", 2020)]
        after = [row("A", 2020), row("B", 2021)]
        assert flow._paper_signature(before) != flow._paper_signature(after)

    def test_편입되면_서명이_바뀐다(self):
        # promote 스크립트는 internal_paper_id를 external_id와 같은 값으로 채운다.
        # 노드 키만 해시하면 편입 전후가 같아져 is_internal=false인 캐시가 굳는다.
        before = [row("ART1", 2020), row("ART2", 2021)]
        after = [row("ART1", 2020, internal="ART1"), row("ART2", 2021)]
        assert flow._paper_signature(before) != flow._paper_signature(after)

    def test_순서가_달라도_같은_서명이다(self):
        a = [row("A", 2020), row("B", 2021)]
        assert flow._paper_signature(a) == flow._paper_signature(list(reversed(a)))


class TestOrderKey:
    def test_과거에서_최신_순으로_정렬된다(self):
        rows = [row("c", 2021, "11"), row("a", 2019, "01"), row("b", 2021, "03")]
        assert [r.external_id for r in sorted(rows, key=flow._order_key)] == ["a", "b", "c"]

    def test_연도가_없으면_맨_앞으로_간다(self):
        rows = [row("a", 2000), row("b", None)]
        assert [r.external_id for r in sorted(rows, key=flow._order_key)] == ["b", "a"]


class TestBuildEdges:
    def test_엣지는_항상_과거에서_최신으로_향한다(self):
        rows = [row("a", 2018), row("b", 2020), row("c", 2022)]
        vectors = np.array([[1.0, 0.0], [0.9, 0.1], [0.8, 0.2]], dtype=np.float32)
        edges = flow._build_edges(rows, vectors, np.array([0, 0, 0]))
        order = {r.external_id: i for i, r in enumerate(rows)}
        assert edges
        for edge in edges:
            assert order[edge.source] < order[edge.target]

    def test_다른_묶음끼리는_잇지_않는다(self):
        rows = [row("a", 2018), row("b", 2020)]
        vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        assert flow._build_edges(rows, vectors, np.array([0, 1])) == []

    def test_공유_키워드를_함께_돌려준다(self):
        rows = [row("a", 2018, keywords=["항산화", "효소"]), row("b", 2020, keywords=["효소", "발효"])]
        vectors = np.array([[1.0, 0.0], [0.9, 0.1]], dtype=np.float32)
        assert flow._build_edges(rows, vectors, np.array([0, 0]))[0].shared_keywords == ["효소"]


class TestCluster:
    def test_논문이_한두_편이면_한_묶음이다(self):
        assert flow._cluster(np.array([[1.0, 0.0]], dtype=np.float32)).tolist() == [0]

    def test_묶음_수는_상한을_넘지_않는다(self):
        vectors = np.random.RandomState(0).rand(200, 8).astype(np.float32)
        labels = flow._cluster(vectors)
        assert len(set(labels.tolist())) <= flow._MAX_CLUSTERS

    def test_큰_묶음_하나가_전부를_먹지_않는다(self):
        # average 연결에서 341편 중 324편(95%)이 한 묶음이 되던 문제 때문에 ward를 쓴다
        vectors = np.random.RandomState(1).rand(120, 16).astype(np.float32)
        sizes = np.bincount(flow._cluster(vectors))
        assert sizes.max() / sizes.sum() < 0.6


class TestParseLLM:
    def test_코드펜스를_벗겨낸다(self):
        raw = '```json\n{"topics": {"0": "효소 분해 연구"}, "summary": "한 문장."}\n```'
        topics, summary = flow._parse_llm(raw)
        assert topics == {0: "효소 분해 연구"}
        assert summary == "한 문장."

    def test_앞뒤에_설명이_붙어도_찾아낸다(self):
        raw = '결과입니다: {"topics": {"1": "주제"}, "summary": "요약"} 이상입니다'
        topics, summary = flow._parse_llm(raw)
        assert topics == {1: "주제"} and summary == "요약"

    def test_JSON이_없으면_예외를_던진다(self):
        # 호출부가 이 예외를 잡아 규칙 기반 문장으로 폴백한다
        with pytest.raises(ValueError):
            flow._parse_llm("JSON 없이 그냥 문장만 왔다")


class TestRuleSummary:
    """LLM이 실패해도 요약 카드는 비울 수 없다 (명세: 상시 노출)."""

    def test_논문이_없으면_문장도_없다(self):
        assert flow._rule_summary([], []) is None

    def test_묶음이_있으면_시작과_최근을_엮어_문장을_만든다(self):
        cluster = ResearchFlowCluster(
            cluster_id=0, topic="효소", topic_keywords=["효소", "발효"], paper_count=2,
            start_paper=ResearchFlowClusterPaper(node_id="a", title="첫 논문", year=2010),
            latest_paper=ResearchFlowClusterPaper(node_id="b", title="최근 논문", year=2020),
            has_followup=True, node_ids=["a", "b"],
        )
        summary = flow._rule_summary([cluster], [row("a", 2010), row("b", 2020)])
        assert summary and "2010" in summary
