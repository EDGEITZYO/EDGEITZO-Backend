import numpy as np
import pytest
from types import SimpleNamespace

from app.schemas.researcher_detail import ResearchFlowCluster, ResearchFlowPaper
from app.services import researcher_flow_service as flow


def row(ext, year, month="01", keywords=None, internal=None, title="제목", journal=None, citations=0):
    return SimpleNamespace(
        external_id=ext, internal_paper_id=internal, title=title, pubyear=year,
        pubmonth=month, pubdate=None, paper_source=None, title_en=None, keywords=keywords or [], authors=[], url=None,
        citation_count=citations, journal=journal,
    )


def cluster(cluster_id, keywords, paper_count, start_year=None, end_year=None, titles=()):
    return ResearchFlowCluster(
        cluster_id=cluster_id, topic=" · ".join(keywords), topic_keywords=keywords,
        paper_count=paper_count, start_year=start_year, end_year=end_year,
        papers=[ResearchFlowPaper(node_id=f"n{i}", title=t, is_internal=False) for i, t in enumerate(titles)],
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

    def test_연도가_없으면_맨_뒤로_간다(self):
        rows = [row("b", None), row("a", 2000)]
        assert [r.external_id for r in sorted(rows, key=flow._order_key)] == ["a", "b"]


class TestOrderLabels:
    """cluster_id가 곧 화면 순서다. 오른쪽 목록 번호와 왼쪽 카드가 같은 번호를 가리켜야 한다.
    기획 명세: 각 카드의 마지막 연구가 최근인 순 (2012~2026이 2021~2024보다 앞)."""

    def test_마지막_연구가_최근인_분야가_0번이다(self):
        # 7번 분야 2012~2026, 3번 분야 2021~2024 → 7번이 먼저
        rows = [row("a", 2012), row("b", 2021), row("c", 2024), row("d", 2026)]
        labels = flow._order_labels(rows, np.array([7, 3, 3, 7]))
        assert labels.tolist() == [0, 1, 1, 0]

    def test_끝_연도가_같으면_마지막_논문이_더_최근인_쪽이_앞이다(self):
        rows = [row("a", 2015), row("b", 2023, "03"), row("c", 2023, "11")]
        labels = flow._order_labels(rows, np.array([5, 5, 9]))
        assert labels.tolist() == [1, 1, 0]

    def test_연도가_전부_없는_분야는_맨_뒤다(self):
        rows = [row("a", 2020), row("b", None)]
        assert flow._order_labels(rows, np.array([4, 2])).tolist() == [0, 1]


class TestBuildClusters:
    def test_카드에_논문_목록과_연도_구간이_담긴다(self):
        rows = [
            row("a", 2019, journal="생체재료학회지", citations=17, title="첫 논문"),
            row("b", 2020, title="다른 분야"),
            row("c", 2021, citations=0, title="둘째 논문"),
        ]
        clusters = flow._build_clusters(rows, np.array([0, 1, 0]))
        first = clusters[0]
        assert [p.title for p in first.papers] == ["첫 논문", "둘째 논문"]
        assert (first.start_year, first.end_year, first.paper_count) == (2019, 2021, 2)
        assert first.papers[0].journal_name == "생체재료학회지"
        assert first.papers[0].citation_count == 17
        assert first.papers[1].citation_count == 0  # 기획 명세: 0이면 0으로 표기
        assert clusters[1].papers[0].title == "다른 분야"


def unit(*vs):
    arr = np.array(vs, dtype=np.float32)
    return arr / np.linalg.norm(arr, axis=1, keepdims=True)


class TestCluster:
    def test_논문이_한_편이면_한_분야다(self):
        assert flow._cluster(unit([1.0, 0.0])).tolist() == [0]

    def test_가까운_논문끼리_묶고_먼_논문은_떼어낸다(self):
        labels = flow._cluster(unit([1.0, 0.0, 0.0], [0.95, 0.05, 0.0], [0.0, 0.0, 1.0]))
        assert labels[0] == labels[1] != labels[2]

    def test_동떨어진_한_편은_흡수하지_않고_따로_둔다(self):
        labels = flow._cluster(unit([1, 0, 0], [0.97, 0.03, 0], [0.95, 0.05, 0], [0, 1, 0]))
        assert list(labels).count(labels[3]) == 1

    def test_분야_수에_상한이_없다(self):
        # 서로 직교하는 논문 12편은 12개 분야다 — 예전에는 최대 6개로 잘렸다
        labels = flow._cluster(np.eye(12, dtype=np.float32))
        assert len(set(labels.tolist())) == 12

    def test_전부_비슷하면_한_분야다(self):
        labels = flow._cluster(unit(*[[1.0, 0.02 * i] for i in range(10)]))
        assert len(set(labels.tolist())) == 1


class TestFlowLevel:
    def test_분야_수로_정한다(self):
        assert flow._flow_level(1, 1) == "none"
        assert flow._flow_level(30, 1) == "single"
        assert flow._flow_level(3, 2) == "flow"


class TestParseLLM:
    def test_코드펜스를_벗겨낸다(self):
        raw = (
            '```json\n{"clusters": {"0": {"topic": "효소 분해 연구", "description": "설명이에요."}}, '
            '"summary": "한 줄."}\n```'
        )
        topics, descriptions, summary = flow._parse_llm(raw)
        assert topics == {0: "효소 분해 연구"}
        assert descriptions == {0: "설명이에요."}
        assert summary == "한 줄."

    def test_앞뒤에_설명이_붙어도_찾아낸다(self):
        raw = '결과입니다: {"clusters": {"분야 1": {"topic": "주제"}}, "summary": "요약"} 이상입니다'
        topics, descriptions, summary = flow._parse_llm(raw)
        assert topics == {1: "주제"} and descriptions == {} and summary == "요약"

    def test_분야명만_문자열로_와도_받는다(self):
        topics, _, _ = flow._parse_llm('{"clusters": {"0": "주제"}, "summary": "요약"}')
        assert topics == {0: "주제"}

    def test_JSON이_없으면_예외를_던진다(self):
        # 호출부가 이 예외를 잡아 규칙 기반 문장으로 폴백한다
        with pytest.raises(ValueError):
            flow._parse_llm("JSON 없이 그냥 문장만 왔다")


class TestUserPrompt:
    def test_분야마다_연도_구간과_논문_제목을_보여준다(self):
        prompt = flow._user_prompt([cluster(0, ["효소"], 2, 2019, 2021, titles=("첫 논문", "둘째 논문"))])
        assert "[분야 0] 논문 2편 (2019~2021년)" in prompt
        assert "- 첫 논문" in prompt and "- 둘째 논문" in prompt

    def test_제목은_연도와_함께_시기_전체에서_고르게_보여준다(self):
        # 앞에서 10편만 자르면 초기 논문만 보여 '흐름' 문장을 쓸 수 없다
        c = cluster(0, ["효소"], 30, 1995, 2024)
        c.papers = [ResearchFlowPaper(node_id=str(y), title=f"논문{y}", pub_year=y, is_internal=False)
                    for y in range(1995, 2025)]
        prompt = flow._user_prompt([c])
        assert "(1995) 논문1995" in prompt and "(2024) 논문2024" in prompt

    def test_출력_한도는_분야_수에_비례하고_상한이_있다(self):
        assert flow._max_tokens(10) > flow._max_tokens(2)
        assert flow._max_tokens(10_000) == flow._MAX_TOKENS_CAP


class TestSummaryLength:
    def test_규칙_요약도_100자를_넘지_않는다(self):
        long_kw = "가" * 40
        summary = flow._rule_summary([cluster(0, [long_kw], 5), cluster(1, [long_kw + "나"], 4), cluster(2, [long_kw + "다"], 3)])
        assert summary is None or len(summary) <= flow._SUMMARY_MAX_CHARS


class TestRuleSummary:
    """LLM이 실패해도 요약 카드는 비울 수 없다 (명세: 상시 노출)."""

    def test_분야가_없으면_문장도_없다(self):
        assert flow._rule_summary([]) is None

    def test_분야가_하나면_대부분이라고_쓴다(self):
        assert "대부분" in flow._rule_summary([cluster(0, ["효소", "발효"], 3)])

    def test_분야가_여럿이면_논문이_많은_순으로_나열한다(self):
        summary = flow._rule_summary(
            [cluster(0, ["효소"], 2), cluster(1, ["발효"], 5), cluster(2, ["항산화"], 1)]
        )
        assert summary.index("발효") < summary.index("효소") < summary.index("항산화")


class TestSingleCard:
    """논문 1편짜리 분야는 제목만 쓰고 설명(AI 요약)을 붙이지 않는다 (기획 확정)."""

    def test_프롬프트에_1편_분야를_제목만이라고_표시한다(self):
        prompt = flow._user_prompt([cluster(0, ["효소"], 1, 2019, 2019, titles=("단일 논문",)),
                                    cluster(1, ["발효"], 3, 2015, 2020, titles=("가", "나", "다"))])
        assert "[분야 0] 논문 1편 — 제목만" in prompt
        assert "[분야 1] 논문 3편 (" in prompt

    @pytest.mark.asyncio
    async def test_모델이_설명을_써도_1편_분야는_버린다(self, monkeypatch):
        rows = [row("a", 2019, keywords=["효소"], title="단일"),
                row("b", 2015, keywords=["발효"], title="가"), row("c", 2020, keywords=["발효"], title="나")]

        async def fake_fetch(db, rid):
            return rows

        async def fake_sentences(clusters):
            return ({c.cluster_id: f"분야{c.cluster_id}" for c in clusters},
                    {c.cluster_id: "설명이에요." for c in clusters}, "요약이에요.", "llm", "m")

        monkeypatch.setattr(flow, "fetch_paper_rows", fake_fetch)
        monkeypatch.setattr(flow, "_embed_and_cluster", lambda rs: np.array([0 if r.external_id == "a" else 1 for r in rs]))
        monkeypatch.setattr(flow, "_write_sentences", fake_sentences)

        async def no_save(*a, **k):
            return None
        monkeypatch.setattr(flow, "_save_cache", no_save)
        result = await flow.get_research_flow(None, "kci:x", use_cache=False)
        by_count = {c.paper_count: c for c in result.clusters}
        assert by_count[1].description is None and by_count[1].topic
        assert by_count[2].description == "설명이에요."
