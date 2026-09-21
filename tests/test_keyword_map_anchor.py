"""키워드맵 앵커 해석 테스트.

키워드맵은 사용자가 입력한 문장(`?keyword=노화관련 연구 찾아줘`)을 앵커로 찾다가 계속 404가 났다.
Neo4j Keyword 노드는 논문 원본 키워드로 만들어져 있어 사용자 어휘·LLM 키워드와 어휘가 다르기
때문이다 — 실측으로 검색이 성공한 턴의 filters.keywords 3개(치매 조기진단/인지기능 저하/
신경영상 바이오마커)가 **하나도** 노드로 존재하지 않았다.

그래서 앵커를 검색 결과 논문들의 원본 키워드에서 역으로 뽑아 key로 내려준다(확장 칩이 이미
쓰던 방식 — `_top_result_keywords` 참고). 문자열 해석 경로는 검색 없이 들어오는 딥링크·세션
재개용 안전망으로만 남긴다.

Neo4j가 필요한 조회는 여기서 가짜 repo로 대체한다 — 검증 대상은 "어느 노드를 앵커로 고르는가"다.
"""
import pytest

from app.langgraph.search_graph import _build_expand_chips_sync, _to_anchor
from app.services.keywords.keyword_db import (
    KEYWORD_ANCHOR_MAX_WORDS,
    KEYWORD_ANCHOR_NAME_MAX_LEN,
    is_usable_anchor_name,
)
from app.services.keywords.text_tokens import extract_nouns

# 적재 때 구분자 분리가 실패해 논문 한 편의 키워드 목록이 통째로 한 노드가 된 실제 사례
_POLLUTED_NAME = (
    "Hypoxia Oncogene induced senescence (OIS) Chromatin accessibility ATAC-seq "
    "Nucleosome-free region (NFR) 저산소증 종양유전자 유도 노화 염색질 접근성"
)


def _node(key, name, lang="ko", paper_count=3):
    return {"key": key, "name": name, "lang": lang, "paper_count": paper_count}


class _FakeRepo:
    def __init__(self, nodes: dict):
        self._nodes = nodes
        self.related_calls: list[str] = []

    def find_keyword(self, keyword, **_):
        return self._nodes.get(keyword)

    def find_related_keywords(self, keyword_key, **_):
        self.related_calls.append(keyword_key)
        return []


class _FakeDriver:
    def close(self):
        pass


@pytest.fixture
def fake_graph(monkeypatch):
    """_build_expand_chips_sync가 함수 안에서 import하는 두 이름을 원본 모듈에서 갈아끼운다."""
    holder = {}

    def _install(nodes: dict):
        repo = _FakeRepo(nodes)
        holder["repo"] = repo
        monkeypatch.setattr("app.core.neo4j_client.get_neo4j_driver", lambda: _FakeDriver())
        monkeypatch.setattr("app.repositories.graph_repository.GraphRepository", lambda driver: repo)
        return repo

    return _install


# ── 앵커 선택 ────────────────────────────────────────────────────────────

def test_처음으로_그래프에서_찾아진_키워드가_앵커가_된다(fake_graph):
    """입력은 결과 논문 키워드를 빈도순으로 받으므로, 먼저 찾아진 것이 결과를 가장 잘 대표한다."""
    fake_graph({"치매": _node("ko:치매", "치매", paper_count=2)})
    _, anchor = _build_expand_chips_sync(["없는키워드", "치매"])
    assert anchor == {"key": "ko:치매", "name_ko": "치매", "name_en": None, "paper_count": 2}


def test_적재_오류로_이름이_긴_노드는_앵커로_쓰지_않는다(fake_graph):
    """이런 노드는 풀텍스트에서 흔한 검색어를 가로채 화면 중앙에 긴 문자열이 박히게 만든다."""
    fake_graph({
        "노화": _node("ko:polluted", _POLLUTED_NAME, paper_count=1),
        "세포": _node("ko:세포", "세포", paper_count=9),
    })
    _, anchor = _build_expand_chips_sync(["노화", "세포"])
    assert anchor["key"] == "ko:세포"


def test_하나도_못_찾으면_앵커는_None(fake_graph):
    fake_graph({})
    chips, anchor = _build_expand_chips_sync(["없는키워드"])
    assert chips == [] and anchor is None


def test_앵커를_뽑아도_확장_칩_조회는_그대로_돈다(fake_graph):
    """앵커는 이미 돌던 find_keyword의 부산물이다 — 칩 로직을 가로채면 안 된다."""
    repo = fake_graph({
        "치매": _node("ko:치매", "치매"),
        "세포": _node("ko:세포", "세포"),
    })
    _build_expand_chips_sync(["치매", "세포"])
    assert repo.related_calls == ["ko:치매", "ko:세포"]


def test_영문_노드는_name_en에_담긴다():
    assert _to_anchor(_node("en:dementia", "Dementia", lang="en")) == {
        "key": "en:dementia", "name_ko": None, "name_en": "Dementia", "paper_count": 3,
    }


# ── 앵커 이름 가드 ───────────────────────────────────────────────────────

def test_단어가_너무_많으면_앵커로_쓰지_않는다():
    """오염 노드의 표식은 길이가 아니라 단어 수다 — 논문 키워드 목록이 통째로 들어간 것."""
    assert is_usable_anchor_name(" ".join(["단어"] * KEYWORD_ANCHOR_MAX_WORDS))
    assert not is_usable_anchor_name(" ".join(["단어"] * (KEYWORD_ANCHOR_MAX_WORDS + 1)))
    assert not is_usable_anchor_name(_POLLUTED_NAME)


def test_정상적인_긴_학술용어는_앵커로_쓸_수_있다():
    """길이로만 자르면 이런 용어까지 빠진다 — 실제 코퍼스에 있는 키워드다."""
    assert is_usable_anchor_name("Saturated-absorption cavity ring-down spectroscopy")
    assert is_usable_anchor_name("Astragalus membranaceus and Zanthoxylum schinifolium 1:1 mix")


def test_띄어쓰기_없는_과도하게_긴_이름도_제외():
    assert not is_usable_anchor_name("가" * (KEYWORD_ANCHOR_NAME_MAX_LEN + 1))
    assert not is_usable_anchor_name("")
    assert not is_usable_anchor_name(None)


# ── 문장 → 명사 폴백 (딥링크·세션 재개 안전망) ──────────────────────────────

def test_탐색_의도_표현은_앵커_후보에서_빠진다():
    """'연구'가 앵커가 되면 주제와 무관한 지도가 그려진다."""
    nouns = extract_nouns("노화 관련 연구 찾아줘")
    assert "연구" not in nouns and "관련" not in nouns
    assert "노화" in nouns


def test_쪼개진_복합명사를_도로_합쳐_먼저_시도한다():
    """kiwi는 "조기진단"을 "조기"+"진단"으로 쪼갠다. 조각만 쓰면 의미가 뭉개진다."""
    assert extract_nouns("치매 조기진단 논문") == ["조기진단", "치매", "조기", "진단"]


def test_불용어가_낀_덩어리는_합치지_않는다():
    """"노화관련"이 후보가 되면 있지도 않은 키워드를 먼저 조회하게 된다."""
    assert extract_nouns("노화관련 연구 찾아줘") == ["노화"]


def test_같은_길이면_원문_순서를_따른다():
    """순서가 결정론적이어야 같은 질의가 늘 같은 앵커로 풀린다."""
    assert extract_nouns("치매 진단 연구") == ["치매", "진단"]


def test_명사가_없으면_빈_목록():
    assert extract_nouns("안녕?") == []


# ── 단계 폴스루 (accept) ──────────────────────────────────────────────────
#
# search_keywords는 풀텍스트에서 뭐라도 잡히면 거기서 끝난다. 호출부가 결과를 받아서
# 거르는 방식이면, 오염 노드 하나만 잡힌 단계가 '성공'으로 처리되어 동의어·임베딩
# 단계에 도달하지 못한다. "노화"가 실제로 그래서 404였다 — "aging"은 Anti-aging으로
# 잘 풀리는데도. 그래서 조건을 함수 안으로 넣었다.

def _record(key, name, lang="ko", paper_count=1):
    return {"k": {"key": key, "name": name, "lang": lang}, "paper_count": paper_count}


@pytest.fixture
def fake_keyword_search(monkeypatch):
    def _install(fulltext: list, embedding: list):
        calls = {"embedding": 0}

        def _ft(ft_query, lang, limit):
            return fulltext

        def _emb(query, lang=None, limit=5):
            calls["embedding"] += 1
            return embedding

        monkeypatch.setattr("app.services.keywords.keyword_db._run_fulltext_query", _ft)
        monkeypatch.setattr("app.services.keywords.keyword_db.embedding_search", _emb)
        return calls

    return _install


def test_풀텍스트가_못_쓸_후보만_주면_임베딩까지_간다(fake_keyword_search):
    from app.services.keywords.keyword_db import search_keywords

    calls = fake_keyword_search(
        fulltext=[_record("ko:polluted", _POLLUTED_NAME)],
        embedding=[{"key": "ko:노인", "name": "노인", "lang": "ko", "paper_count": 4}],
    )
    got = search_keywords("노화", accept=lambda k: is_usable_anchor_name(k.name_ko or k.name_en))
    assert [k.key for k in got] == ["ko:노인"]
    assert calls["embedding"] == 1


def test_accept가_없으면_기존_동작_그대로(fake_keyword_search):
    """다른 호출부(키워드 검색·확장 칩)의 동작은 바뀌면 안 된다."""
    from app.services.keywords.keyword_db import search_keywords

    calls = fake_keyword_search(
        fulltext=[_record("ko:polluted", _POLLUTED_NAME)],
        embedding=[{"key": "ko:노인", "name": "노인", "lang": "ko"}],
    )
    got = search_keywords("노화")
    assert [k.key for k in got] == ["ko:polluted"]
    assert calls["embedding"] == 0
