"""연구 흐름 시각화 + 요약 카드 (명세 08-05 / 08-06).

묶고 잇는 일은 계산이 하고, LLM은 문장화만 한다 — 명세 08-06의 원칙 그대로다.
("키워드 및 변화 판단은 논문 데이터 기반이며 AI는 문장화만 담당")

왜 키워드 글자 일치로 잇지 않는가:
  명세 원안은 "키워드 공유"인데, 한 연구자의 논문 쌍 중 80~99%가 공유 0으로 나온다(실측).
  '효소 분해'와 'Alcalase-enzymatic hydrolysate'처럼 같은 연구인데 표기가 다르면 안 잡힌다.
  그래서 제목+키워드를 BGE-m3-ko로 임베딩해 의미 유사도로 잇는다.

왜 유사도 임계값을 고정하지 않는가:
  논문 단위 임베딩은 연구자마다 유사도 분포가 다르다(p90이 0.49~0.52로 흔들린다).
  0.7 같은 고정 임계값을 쓰면 노드의 77~100%가 고립돼 화면이 텅 빈다(실측).
  그래서 절대값 대신 **상대 구조**를 쓴다 — 묶음은 클러스터링으로, 선은 각 논문에서
  '같은 묶음의 직전 논문 중 가장 가까운 것'으로 잇는다. 고립 노드가 0~9%로 떨어진다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from typing import Any, Optional

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.schemas.researcher_detail import (
    ResearchFlowCluster,
    ResearchFlowClusterPaper,
    ResearchFlowEdge,
    ResearchFlowNode,
    ResearchFlowResponse,
)
from app.services.researcher_detail_service import fetch_paper_rows

logger = logging.getLogger(__name__)

# 클러스터링 규칙이나 프롬프트를 바꾸면 올린다 — 기존 캐시가 자동으로 재생성된다.
# v3: _PAPERS_PER_CLUSTER 4→3, 1편 묶음 흡수(_absorb_small) 추가 (2026-09-23)
PROMPT_VERSION = "v3"

# 요약 카드 개수. 와이어프레임이 4장 안팎이고, 카드가 너무 잘게 쪼개지면
# "연구 흐름"이 아니라 논문 목록이 된다.
#
# 4에서 3으로 내렸다(2026-09-23). k = round(n/4)라 5편 이하는 묶음이 무조건 1개가 되어
# "분야 단위 패널"이 성립하지 않았다. 연구자 150명·논문 2,516편 표본 실측:
#
#   논문 수   ppc=4              ppc=3 + 1편 묶음 흡수
#   3-5편     1.0묶음            1.1묶음 · 실루엣 0.195 · 키워드 분리 8.4배
#   8-14편    2.7묶음 (1편 8%)   3.1묶음 (1편 0%)
#   15-29편   5.2묶음 (1편 2%)   5.6묶음 (1편 0%)
#
# ppc=2도 재봤으나 6-7편 구간에서 키워드 분리배수가 12.1→3.3으로 무너져 채택하지 않았다.
_PAPERS_PER_CLUSTER = 3
_MAX_CLUSTERS = 6

# 논문 1편짜리 묶음은 카드 한 장에 논문 한 편이라 "흐름"이 아니라 목록이다.
# 이보다 작은 묶음은 가장 가까운 묶음에 흡수시킨다.
_MIN_CLUSTER_SIZE = 2

_MODEL = settings.llm_model_fast
_MAX_TOKENS = 900


def _order_key(row: Any) -> tuple[int, int]:
    """과거 → 최신. KCI가 일자를 주지 않아 같은 달 안의 순서는 정해지지 않는다."""
    month = int(row.pubmonth) if row.pubmonth and str(row.pubmonth).isdigit() else 0
    return (row.pubyear or 0, month)


def _node_id(row: Any) -> str:
    return row.internal_paper_id or row.external_id or ""


def _flow_level(paper_count: int) -> str:
    """이 논문 수로 무엇까지 말할 수 있는지. 묶음 공식에서 그대로 따라 나온다.

    k = round(n / _PAPERS_PER_CLUSTER)이므로 ppc=3에서는 5편부터 2묶음이 가능하다.
    4편까지는 계산상 반드시 1묶음이라 "분야가 옮겨갔다"고 말할 수 없다 — 그런데도
    지금까지 같은 응답을 내보내서, 화면에는 "연구 흐름"이라 써 있고 내용은 논문 목록이었다.
    """
    if paper_count <= 1:
        return "none"
    if paper_count < _PAPERS_PER_CLUSTER * 2 - 1:  # ppc=3 → 5편 미만
        return "single"
    return "flow"


def _paper_signature(rows: list[Any]) -> str:
    """캐시 무효화 키. 논문 목록이 바뀌면 값이 달라진다.

    편입 여부(internal_paper_id)도 같이 넣는다. papers 편입은 internal_paper_id를
    external_id와 같은 값(KCI art_id)으로 채우기 때문에, 노드 키만으로 해시하면
    편입 전후의 서명이 같아진다. 그러면 편입이 끝나도 캐시가 is_internal=false인 옛 응답을
    계속 돌려준다 — 노드를 눌러도 논문 상세로 못 가는 상태가 고정된다.
    """
    keys = sorted(f"{_node_id(r)}:{1 if r.internal_paper_id else 0}" for r in rows)
    return hashlib.sha1("|".join(keys).encode("utf-8")).hexdigest()


def _embed(rows: list[Any]) -> np.ndarray:
    from app.services.embedding_model import get_bge_model

    texts = [
        f"{r.title or ''} {' '.join((r.keywords or [])[:10])}".strip() or "제목 없음"
        for r in rows
    ]
    return get_bge_model().encode(
        texts, convert_to_numpy=True, batch_size=32, normalize_embeddings=True
    )


def _cluster(vectors: np.ndarray) -> np.ndarray:
    """묶음 나누기. ward 연결을 쓴다.

    average/cosine은 응집도가 높은 대신 한 덩어리로 뭉친다 — 341편 연구자에서 상위 묶음이
    324편(95%)을 차지해, '연구 흐름 요약'이 아니라 논문 목록 전체를 가리키는 카드가 나왔다.
    ward는 묶음 크기를 고르게 나눠(최대 묶음 30%) 카드가 각각 의미를 갖는다.
    입력 벡터가 L2 정규화돼 있어 유클리드 거리가 코사인과 단조 관계라 ward를 그대로 쓸 수 있다.
    """
    n = len(vectors)
    if n <= 2:
        return np.zeros(n, dtype=int)
    from sklearn.cluster import AgglomerativeClustering

    k = max(1, min(_MAX_CLUSTERS, round(n / _PAPERS_PER_CLUSTER), n))
    labels = AgglomerativeClustering(n_clusters=k, linkage="ward").fit_predict(vectors)
    return _absorb_small(labels, vectors)


def _absorb_small(labels: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    """_MIN_CLUSTER_SIZE 미만인 묶음을 중심이 가장 가까운 묶음에 흡수시킨다.

    ward가 고른 크기로 나눠주긴 하지만 주제가 동떨어진 논문 한 편은 그대로 홀로 남는다.
    그 카드는 "연구 흐름"이 아니라 논문 한 편을 가리키는 제목표라 화면에서 값이 없다.
    표본 실측으로 1편 묶음이 6-7편 구간 11% · 8-14편 8%였고, 흡수를 넣으면 전 구간 0%가 된다.

    한 번에 하나씩 흡수하고 다시 센다 — 작은 묶음 둘이 서로를 최근접으로 지목하면
    한꺼번에 처리할 때 둘 다 사라지거나 엉뚱하게 합쳐진다.
    """
    labels = labels.copy()
    while True:
        sizes = Counter(labels.tolist())
        if len(sizes) <= 1:
            return labels
        small = [lab for lab, count in sizes.items() if count < _MIN_CLUSTER_SIZE]
        if not small:
            return labels
        centroids = {lab: vectors[labels == lab].mean(axis=0) for lab in sizes}
        source = small[0]
        target = max(
            (lab for lab in sizes if lab != source),
            key=lambda lab: float(centroids[source] @ centroids[lab]),
        )
        labels[labels == source] = target


def _embed_and_cluster(rows: list[Any]) -> tuple[np.ndarray, np.ndarray]:
    """스레드에서 한 번에 돌리는 무거운 계산 두 가지."""
    vectors = _embed(rows)
    return vectors, _cluster(vectors)


def _build_edges(
    rows: list[Any], vectors: np.ndarray, labels: np.ndarray
) -> list[ResearchFlowEdge]:
    """각 논문을 '같은 묶음의 앞선 논문 중 가장 가까운 것'과 잇는다.

    묶음마다 과거에서 최신으로 흐르는 사슬이 하나 생긴다. 전부 잇지 않는 이유는
    40편이면 선이 780개가 되어 화면이 까맣게 되기 때문이다(연구자 그래프에서 겪은 것과 같다).
    """
    sim = vectors @ vectors.T
    edges: list[ResearchFlowEdge] = []
    for i in range(len(rows)):
        earlier = [j for j in range(i) if labels[j] == labels[i]]
        if not earlier:
            continue
        j = max(earlier, key=lambda x: sim[i][x])
        shared = sorted(
            {k.lower() for k in (rows[i].keywords or [])}
            & {k.lower() for k in (rows[j].keywords or [])}
        )
        edges.append(
            ResearchFlowEdge(
                source=_node_id(rows[j]),
                target=_node_id(rows[i]),
                weight=round(float(sim[i][j]), 4),
                shared_keywords=shared[:5],
            )
        )
    return edges


def _cluster_keywords(rows: list[Any], indices: list[int], limit: int = 6) -> list[str]:
    """묶음을 대표하는 실제 키워드. 빈도 우선, 동률이면 먼저 나온 순."""
    counts: dict[str, int] = {}
    for i in indices:
        for kw in rows[i].keywords or []:
            counts[kw] = counts.get(kw, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def _cluster_paper(rows: list[Any], index: int) -> ResearchFlowClusterPaper:
    row = rows[index]
    return ResearchFlowClusterPaper(
        node_id=_node_id(row),
        paper_id=row.internal_paper_id,
        title=row.title,
        year=row.pubyear,
    )


def _rule_topic(keywords: list[str]) -> str:
    """LLM 없이 쓰는 주제명. 키워드 나열이라 화면 문구와는 다르지만 틀린 말은 없다."""
    return " · ".join(keywords[:3]) if keywords else "주제 미상"


def _rule_summary(clusters: list[ResearchFlowCluster], rows: list[Any]) -> Optional[str]:
    if not rows:
        return None
    years = [r.pubyear for r in rows if r.pubyear]
    if len(rows) == 1 or not clusters:
        first = clusters[0].topic_keywords[:2] if clusters else []
        return f"{min(years) if years else '연도 미상'}년 {' · '.join(first)} 연구 1편이 확인됩니다."
    oldest = min(clusters, key=lambda c: c.start_paper.year or 9999 if c.start_paper else 9999)
    newest = max(clusters, key=lambda c: c.latest_paper.year or 0 if c.latest_paper else 0)
    return (
        f"{oldest.start_paper.year if oldest.start_paper else '초기'}년 "
        f"{_rule_topic(oldest.topic_keywords)} 연구에서 출발해, 최근에는 "
        f"{_rule_topic(newest.topic_keywords)} 쪽 연구가 이어지고 있습니다."
    )


def _system_prompt() -> str:
    return (
        "너는 국내 학술 데이터베이스의 연구자 프로필을 쓰는 편집자다.\n"
        "주어진 '연구 묶음'마다 한국어 주제명을 한 줄로 붙이고, 연구자의 관심 분야 변화를 "
        "한 문장으로 정리한다.\n\n"
        "반드시 지킬 것:\n"
        "1. 제시된 키워드와 논문 제목에 실제로 있는 내용만 쓴다. 없는 주제·분야·성과를 "
        "지어내지 않는다.\n"
        "2. 주제명은 명사구로 25자 이내. 예: '오가노이드 기반 재생의학 및 약물 독성 평가 연구'\n"
        "3. 요약은 정확히 한 문장. 연도와 주제 변화가 드러나게 쓴다.\n"
        "4. 연구자 이름·소속·평가(우수한, 활발한 등)는 쓰지 않는다.\n"
        "5. 묶음이 하나뿐이면 변화가 아니라 그 주제가 이어져 왔다고 쓴다.\n\n"
        '출력은 JSON만: {"topics": {"<묶음번호>": "<주제명>"}, "summary": "<한 문장>"}'
    )


def _user_prompt(clusters: list[ResearchFlowCluster], rows: list[Any], labels: np.ndarray) -> str:
    blocks = []
    for cluster in clusters:
        indices = [i for i in range(len(rows)) if labels[i] == cluster.cluster_id]
        titles = [rows[i].title for i in indices if rows[i].title][:3]
        years = [rows[i].pubyear for i in indices if rows[i].pubyear]
        span = f" ({min(years)}~{max(years)}년)" if years else ""
        blocks.append(
            f"[묶음 {cluster.cluster_id}] 논문 {cluster.paper_count}편{span}\n"
            f"  키워드: {', '.join(cluster.topic_keywords) or '없음'}\n"
            f"  논문 제목: {' / '.join(titles) or '제목 없음'}"
        )
    return "다음 연구자의 연구 묶음이다.\n\n" + "\n\n".join(blocks)


def _parse_llm(raw: str) -> tuple[dict[int, str], Optional[str]]:
    body = raw.strip()
    if body.startswith("```"):
        body = body.split("```")[1] if "```" in body[3:] else body.strip("`")
        body = body.removeprefix("json").strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("JSON 객체를 찾지 못함")
    data = json.loads(body[start : end + 1])
    topics = {}
    for key, value in (data.get("topics") or {}).items():
        cluster_id = _cluster_id_from_key(key)
        if cluster_id is not None and value:
            topics[cluster_id] = str(value).strip()
    summary = (data.get("summary") or "").strip() or None
    return topics, summary


def _cluster_id_from_key(raw: Any) -> Optional[int]:
    """주제 키에서 묶음 번호를 꺼낸다.

    int(k)를 바로 부르면 안 된다 — 사용자 프롬프트가 묶음을 `[묶음 0]`, `[묶음 1]`로
    표시하기 때문에 모델이 그 라벨을 그대로 키로 돌려주는 경우가 있다("묶음 1").
    그러면 ValueError가 나고 **주제명과 요약 문장이 통째로 버려져** 규칙 기반으로 폴백한다.
    응답 자체는 멀쩡한데 파서가 못 읽어서 버리는 게 가장 아까운 실패라(§13과 같은 교훈),
    숫자만 뽑아 쓴다. 2026-09-23 예열에서 2,742명 중 11명이 이 경우였다.
    """
    match = re.search(r"-?\d+", str(raw))
    return int(match.group()) if match else None


async def _write_sentences(
    clusters: list[ResearchFlowCluster], rows: list[Any], labels: np.ndarray
) -> tuple[dict[int, str], Optional[str], str, Optional[str]]:
    """LLM에게 주제명과 요약 문장만 맡긴다. 실패하면 규칙 기반으로 폴백한다 —
    명세가 요약 카드를 '상시 노출'로 요구하므로 문장이 없다고 화면을 비울 수는 없다."""
    from app.services.llm.client import LLMBudgetExceededError, LLMRefusalError, chat

    try:
        response = await chat(
            messages=[{"role": "user", "content": _user_prompt(clusters, rows, labels)}],
            model=_MODEL,
            system=_system_prompt(),
            temperature=0.3,
            max_tokens=_MAX_TOKENS,
        )
        topics, summary = _parse_llm(response.text)
        return topics, summary, "llm", response.model
    except LLMBudgetExceededError:
        logger.warning("연구 흐름 요약: 이번 달 LLM 예산 소진 — 규칙 기반으로 폴백")
    except LLMRefusalError:
        logger.warning("연구 흐름 요약: 모델이 응답을 거부 — 규칙 기반으로 폴백")
    except Exception as exc:  # 파싱 실패·네트워크 오류 등
        logger.warning("연구 흐름 요약 생성 실패(%s) — 규칙 기반으로 폴백", type(exc).__name__)
    return {}, None, "rule", None


def _build_clusters(
    rows: list[Any], vectors: np.ndarray, labels: np.ndarray
) -> tuple[list[ResearchFlowCluster], set[int]]:
    """묶음 카드와, 각 묶음의 대표 논문 인덱스(화면의 진한 초록)를 함께 돌려준다."""
    clusters: list[ResearchFlowCluster] = []
    core_indices: set[int] = set()
    for label in sorted(set(labels.tolist())):
        indices = [i for i in range(len(rows)) if labels[i] == label]
        keywords = _cluster_keywords(rows, indices)
        centroid = vectors[indices].mean(axis=0)
        core = max(indices, key=lambda i: float(vectors[i] @ centroid))
        clusters.append(
            ResearchFlowCluster(
                cluster_id=int(label),
                topic=_rule_topic(keywords),
                topic_keywords=keywords,
                paper_count=len(indices),
                start_paper=_cluster_paper(rows, indices[0]),
                latest_paper=_cluster_paper(rows, indices[-1]) if len(indices) > 1 else None,
                has_followup=len(indices) > 1,
                node_ids=[_node_id(rows[i]) for i in indices],
            )
        )
        core_indices.add(core)
    clusters.sort(key=lambda c: -c.paper_count)
    return clusters, core_indices


async def _load_cache(
    db: AsyncSession, researcher_id: str, signature: str
) -> Optional[ResearchFlowResponse]:
    row = (
        await db.execute(
            text(
                "SELECT payload, paper_signature FROM researcher_flow_cache "
                "WHERE researcher_id = :rid AND prompt_version = :ver"
            ),
            {"rid": researcher_id, "ver": PROMPT_VERSION},
        )
    ).first()
    if row is None or row.paper_signature != signature:
        return None
    return ResearchFlowResponse(researcher_id=researcher_id, **row.payload)


async def _save_cache(
    db: AsyncSession, researcher_id: str, signature: str, result: ResearchFlowResponse, model: Optional[str]
) -> None:
    payload = result.model_dump(mode="json")
    payload.pop("researcher_id", None)
    await db.execute(
        text(
            "INSERT INTO researcher_flow_cache "
            "  (researcher_id, prompt_version, paper_signature, payload, summary_source, model, paper_count) "
            "VALUES (:rid, :ver, :sig, CAST(:payload AS jsonb), :src, :model, :cnt) "
            "ON CONFLICT (researcher_id, prompt_version) DO UPDATE SET "
            "  paper_signature = EXCLUDED.paper_signature, payload = EXCLUDED.payload, "
            "  summary_source = EXCLUDED.summary_source, model = EXCLUDED.model, "
            "  paper_count = EXCLUDED.paper_count, created_at = now()"
        ),
        {
            "rid": researcher_id,
            "ver": PROMPT_VERSION,
            "sig": signature,
            "payload": json.dumps(payload, ensure_ascii=False),
            "src": result.summary_source,
            "model": model,
            "cnt": result.total_papers,
        },
    )
    await db.commit()


async def get_research_flow(
    db: AsyncSession, researcher_id: str, *, use_cache: bool = True
) -> ResearchFlowResponse:
    """논문 수와 무관하게 항상 응답한다(명세: 그래프·요약 카드 상시 노출)."""
    rows = sorted(await fetch_paper_rows(db, researcher_id), key=_order_key)
    if not rows:
        return ResearchFlowResponse(
            researcher_id=researcher_id,
            total_papers=0,
            flow_level="none",
            summary=None,
            summary_source="none",
            nodes=[],
            edges=[],
            clusters=[],
        )

    signature = _paper_signature(rows)
    if use_cache:
        cached = await _load_cache(db, researcher_id, signature)
        if cached is not None:
            return cached

    # 임베딩과 클러스터링은 CPU를 오래 잡는 동기 코드다. 그대로 await 없이 부르면
    # 이벤트 루프가 멈춰 그동안 들어온 다른 요청까지 같이 밀린다
    # (실측: 73편 생성 중 프로필 조회가 1~3ms → 51ms). 스레드로 내보낸다.
    vectors, labels = await asyncio.to_thread(_embed_and_cluster, rows)
    clusters, core_indices = _build_clusters(rows, vectors, labels)
    edges = _build_edges(rows, vectors, labels)

    topics, summary, source, model = await _write_sentences(clusters, rows, labels)
    for cluster in clusters:
        if cluster.cluster_id in topics:
            cluster.topic = topics[cluster.cluster_id]
    if summary is None:
        summary = _rule_summary(clusters, rows)
        source = "rule" if source == "llm" else source

    nodes = [
        ResearchFlowNode(
            node_id=_node_id(row),
            paper_id=row.internal_paper_id,
            external_id=row.external_id,
            title=row.title,
            authors=[a for a in (row.authors or []) if a],
            pub_year=row.pubyear,
            pub_month=row.pubmonth,
            published_at=(
                f"{row.pubyear}-{str(row.pubmonth).zfill(2)}"
                if row.pubyear and row.pubmonth
                else (str(row.pubyear) if row.pubyear else None)
            ),
            cluster_id=int(labels[i]),
            is_core=i in core_indices,
            is_internal=row.internal_paper_id is not None,
            external_url=row.url,
            citation_count=row.citation_count if row.citation_count else None,
        )
        for i, row in enumerate(rows)
    ]

    result = ResearchFlowResponse(
        researcher_id=researcher_id,
        total_papers=len(rows),
        flow_level=_flow_level(len(rows)),
        summary=summary,
        summary_source=source,
        nodes=nodes,
        edges=edges,
        clusters=clusters,
    )
    try:
        await _save_cache(db, researcher_id, signature, result, model)
    except Exception:
        logger.exception("연구 흐름 캐시 저장 실패 — 응답에는 영향 없음")
        await db.rollback()
    return result
