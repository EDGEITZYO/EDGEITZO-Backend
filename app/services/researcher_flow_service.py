"""연구 흐름 — 분야 카드 + 요약 (명세 08-05 / 08-06).

연구자의 논문을 의미가 가까운 것끼리 '분야'로 묶고, 분야마다 논문 목록과 설명 한 줄,
전체에 대한 한 줄 요약을 낸다. 묶는 일은 계산이 하고 LLM은 문장화만 한다 —
명세 08-06의 원칙 그대로다("키워드 및 변화 판단은 논문 데이터 기반이며 AI는 문장화만 담당").

왜 키워드 글자 일치로 묶지 않는가:
  명세 원안은 "키워드 공유"인데, 한 연구자의 논문 쌍 중 80~99%가 공유 0으로 나온다(실측).
  '효소 분해'와 'Alcalase-enzymatic hydrolysate'처럼 같은 연구인데 표기가 다르면 안 잡힌다.
  그래서 제목+키워드를 BGE-m3-ko로 임베딩해 의미 유사도로 묶는다.

분야 경계는 묶음 평균 유사도에 임계값을 둔다(_MERGE_DISTANCE). 논문 한 쌍의 유사도는
연구자마다 분포가 흔들려 고정 임계값으로 쓰기 어렵지만, 묶음 전체의 평균은 그보다 안정적이다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Optional

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.schemas.researcher_detail import (
    ResearchFlowCluster,
    ResearchFlowPaper,
    ResearchFlowResponse,
)
from app.services.researcher_detail_service import _published_at, fetch_paper_rows

logger = logging.getLogger(__name__)

# 클러스터링 규칙이나 프롬프트를 바꾸면 올린다 — 기존 캐시가 자동으로 재생성된다.
# v3: _PAPERS_PER_CLUSTER 4→3, 1편 묶음 흡수(_absorb_small) 추가 (2026-09-23)
# v4: 개수 공식(round(n/3), 최대 6) → 유사도 임계값, 1편 분야 허용, 분야 설명·논문 목록 추가 (2026-09-29)
# v5: 카드 순서(마지막 연구가 최근인 순), 카드 설명을 흐름 문장으로, 인용 0 표기, 요약 100자 (2026-09-29)
# v6: 논문 1편짜리 분야는 제목만 쓰고 설명(description)을 만들지 않는다 — 기획 확정 (2026-09-29)
PROMPT_VERSION = "v6"

# 두 묶음을 같은 분야로 합치는 기준: 두 묶음 논문 사이 코사인 거리의 평균이 이 값 미만.
# 즉 평균 유사도가 0.40 이상이면 같은 분야다. 논문 수로 개수를 정하지 않는다 —
# 분야는 내용으로 나뉘어야 하고, 개수 상한도 없다(2026-09-29 기획 확정).
#
# 값은 사람 판단이 아니라 데이터에 이미 있는 근거로 골랐다. 연구자 130명 표본에서
# 같은 연구자의 논문 쌍에 정답을 자동으로 붙이고, 기준값마다 맞힌 비율을 쟀다.
#   같아야 할 쌍(6,421) — 흔하지 않은 저자 키워드를 공유 (전체 논문 0.1% 이하에 나오는 키워드)
#   달라야 할 쌍(78,334) — KCI 학문 분류가 다름
#
#   거리   같아야 할 쌍   달라야 할 쌍   균형 정확도     (입력: 영문 제목 + 키워드)
#   0.55   59.1%         91.6%         75.4%
#   0.58   70.0%         84.9%         77.4%
#   0.60   75.1%         80.9%         78.0%   ← 최대
#   0.62   80.0%         71.4%         75.7%
#   0.65   94.9%         50.1%         72.5%
#
# 정답 쌍에 키워드를 썼으므로 입력에도 키워드가 있으면 순환이 된다. 키워드를 뺀 영문 제목만으로
# 재도 최적점이 0.60~0.63(균형 73~74%)으로 같아 순환 때문에 나온 값이 아니다.
# 0.60에서 분야 수: 5-9편 3.1개 / 10-19편 4.0개 / 20-49편 9.3개 / 50편+ 15.6개,
# 1편 분야 비율 32~41%. 1편 분야는 실제로 동떨어진 논문이라 흡수하지 않는다(기획 결정).
#
# ward는 쓰지 않는다. ward의 합병 비용은 묶음이 클수록 커져서, 같은 임계값이라도 논문이 많은
# 연구자일수록 분야가 잘게 쪼개진다 — 내용이 아니라 개수에 끌려가는 방식이다.
_MERGE_DISTANCE = 0.60

_MODEL = settings.llm_model_fast
_MAX_TOKENS_BASE = 400
_MAX_TOKENS_CAP = 8000


def _order_key(row: Any) -> tuple[bool, int, int]:
    """과거 → 최신, 연도 미상은 끝. KCI가 일자를 주지 않아 같은 달 안의 순서는 정해지지 않는다."""
    month = int(row.pubmonth) if row.pubmonth and str(row.pubmonth).isdigit() else 0
    return (row.pubyear is None, row.pubyear or 0, month)


def _node_id(row: Any) -> str:
    return row.internal_paper_id or row.external_id or ""


def _flow_level(paper_count: int, cluster_count: int) -> str:
    """이 결과로 무엇까지 말할 수 있는지. 논문 수가 아니라 실제로 나뉜 분야 수로 정한다."""
    if paper_count <= 1:
        return "none"
    if cluster_count <= 1:
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
    """영문 제목 + 키워드로 임베딩한다.

    원래 제목을 쓰면 한글 논문과 영문 논문이 같은 주제여도 언어 차이로 멀어져 다른 분야로
    갈라졌다. 영문 제목은 연구자 논문의 99.4%에 있어(영문 논문은 원제목 자체가 영문) 모든 논문이
    같은 언어로 비교된다. 한글+영문 제목을 함께 넣는 방식은 영문 논문에 한글 제목이 없어 언어
    차이가 남았다. 위 _MERGE_DISTANCE 실측에서 영문 제목 입력이 모든 기준값에서 1~3%p 앞섰다.
    영문 제목이 없는 0.6%는 원래 제목을 쓴다. 초록은 70%에만 있어 넣지 않는다.
    """
    from app.services.embedding_model import get_bge_model

    texts = [
        f"{r.title_en or r.title or ''} {' '.join((r.keywords or [])[:10])}".strip() or "제목 없음"
        for r in rows
    ]
    return get_bge_model().encode(
        texts, convert_to_numpy=True, batch_size=32, normalize_embeddings=True
    )


def _cluster(vectors: np.ndarray) -> np.ndarray:
    """분야 나누기. average 연결 + 코사인 거리 임계값으로, 개수는 데이터가 정한다."""
    n = len(vectors)
    if n <= 1:
        return np.zeros(n, dtype=int)
    from sklearn.cluster import AgglomerativeClustering

    return AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=_MERGE_DISTANCE,
        linkage="average",
        metric="cosine",
    ).fit_predict(vectors)


def _embed_and_cluster(rows: list[Any]) -> np.ndarray:
    """스레드에서 한 번에 돌리는 무거운 계산 두 가지. 벡터는 묶는 데만 쓰고 버린다."""
    return _cluster(_embed(rows))


def _cluster_keywords(rows: list[Any], indices: list[int], limit: int = 6) -> list[str]:
    """묶음을 대표하는 실제 키워드. 빈도 우선, 동률이면 먼저 나온 순."""
    counts: dict[str, int] = {}
    for i in indices:
        for kw in rows[i].keywords or []:
            counts[kw] = counts.get(kw, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def _flow_paper(row: Any) -> ResearchFlowPaper:
    return ResearchFlowPaper(
        node_id=_node_id(row),
        paper_id=row.internal_paper_id,
        external_id=row.external_id,
        title=row.title,
        journal_name=row.journal,
        # 기획 명세: 인용수를 못 불러오면 표기하지 않고, 0이면 0으로 표기한다 — 값이 없을 때만 null.
        # (08-02 논문 리스트는 KCI 0을 '미집계일 수 있음'으로 보고 null로 보낸다. 규칙이 다르다.)
        citation_count=row.citation_count,
        pub_year=row.pubyear,
        published_at=_published_at(row.pubyear, row.pubmonth, row.pubdate, row.paper_source),
        is_internal=row.internal_paper_id is not None,
        external_url=row.url,
    )


def _rule_topic(keywords: list[str]) -> str:
    """LLM 없이 쓰는 주제명. 키워드 나열이라 화면 문구와는 다르지만 틀린 말은 없다."""
    return " · ".join(keywords[:3]) if keywords else "주제 미상"


# 기획 명세: 전체 한 줄 요약은 100자 이하.
_SUMMARY_MAX_CHARS = 100


def _rule_summary(clusters: list[ResearchFlowCluster]) -> Optional[str]:
    """LLM 없이 쓰는 전체 한 줄. 논문 수가 많은 분야의 대표 키워드를 나열한다(100자 이하)."""
    if not clusters:
        return None
    if len(clusters) == 1:
        keywords = clusters[0].topic_keywords[:2]
        if not keywords:
            return None
        sentence = f"{' · '.join(keywords)} 관련 연구가 대부분이에요."
        return sentence if len(sentence) <= _SUMMARY_MAX_CHARS else f"{keywords[0]} 관련 연구가 대부분이에요."
    ranked = sorted(clusters, key=lambda c: -c.paper_count)
    heads = [c.topic_keywords[0] for c in ranked if c.topic_keywords][:3]
    while heads:
        sentence = f"주로 {', '.join(heads)} 관련 연구를 해왔어요."
        if len(sentence) <= _SUMMARY_MAX_CHARS:
            return sentence
        heads = heads[:-1]
    return None


def _system_prompt() -> str:
    return (
        "너는 국내 학술 데이터베이스에서 한 연구자의 논문을 분야별로 정리하는 편집자다.\n"
        "논문은 이미 의미가 가까운 것끼리 '분야'로 묶여 있다. 너는 묶음을 바꾸지 않고 문장만 쓴다.\n\n"
        "분야마다 쓸 것:\n"
        "- topic: 분야명. 명사구로 25자 이내. 예: '역분화줄기세포 분화 조건 최적화'\n"
        "- description: 이 분야 안에서 연구가 **시간에 따라 어떻게 흘러왔는지** 한 문장, 70자 이내, 해요체. "
        "논문은 연도순으로 주어진다. 앞 시기와 뒤 시기의 주제를 비교해서 쓴다.\n"
        "  반드시 아래 세 형태 중 하나로 쓰고, 끝맺음 말은 **글자 그대로** 쓴다('확대됐어요'·'변화했어요' 등으로 바꾸지 않는다).\n"
        "  · 주제가 옮겨 갔으면: '<앞 시기 주제> 연구로 시작해 <뒤 시기 주제>로 변화한 흐름을 보여요.'\n"
        "  · 같은 주제가 이어지면: '<주제>에 관련한 연구를 지속해서 진행하고 있어요.'\n"
        "  · **논문이 1편뿐인 분야(입력에 '1편 — 제목만'으로 표시)는 description을 쓰지 않는다.** topic만 쓴다.\n\n"
        "전체로 쓸 것:\n"
        "- summary: 이 연구자의 논문 전체가 주로 어떤 연구인지 한 줄, 공백 포함 100자 이하(가급적 80자 안), 해요체. "
        "논문 수가 많은 분야를 중심으로 쓰고, 분야가 많아도 **대표 분야 3개까지만** 담는다 — "
        "전부 나열하지 않는다. 'A, B, C 연구를 주로 해왔어요'처럼 나열해도 되고, "
        "대부분 한 분야면 '~ 연구가 대부분이에요'처럼 쓴다. "
        "연도에 따른 변화는 억지로 만들지 않는다.\n\n"
        "반드시 지킬 것:\n"
        "1. 제시된 키워드와 논문 제목에 실제로 있는 내용만 쓴다. 없는 주제·방법·성과를 지어내지 않는다.\n"
        "2. 연구자 이름·소속·평가(우수한, 활발한, 선도적인 등)는 쓰지 않는다.\n"
        "3. 분야명은 서로 겹치지 않게, 그 분야를 다른 분야와 구분하는 말로 쓴다.\n"
        "4. 논문이 1편뿐인 분야의 topic은 다른 분야와 같은 방식(명사구 분야명)으로 그 논문의 주제를 쓴다.\n\n"
        '출력은 JSON만: {"clusters": {"<분야번호>": {"topic": "<분야명>", "description": "<한 문장>"}}, '
        '"summary": "<한 줄>"}'
    )


# 분야 하나에 LLM에게 보여줄 논문 제목 수. 분야명·설명의 근거라 너무 적으면 한두 편에 끌려간다.
_TITLES_PER_CLUSTER = 10


def _spread(papers: list, limit: int) -> list:
    """시기 전체에 고르게 뽑는다. 앞에서부터 자르면 초기 논문만 보여서 '흐름'을 쓸 수 없다."""
    if len(papers) <= limit:
        return papers
    step = (len(papers) - 1) / (limit - 1)
    return [papers[round(i * step)] for i in range(limit)]


def _user_prompt(clusters: list[ResearchFlowCluster]) -> str:
    total = sum(c.paper_count for c in clusters)
    blocks = []
    for cluster in clusters:
        titled = _spread([p for p in cluster.papers if p.title], _TITLES_PER_CLUSTER)
        titles = [f"({p.pub_year}) {p.title}" if p.pub_year else p.title for p in titled]
        span = (
            f" ({cluster.start_year}~{cluster.end_year}년)"
            if cluster.start_year and cluster.end_year
            else ""
        )
        single = " — 제목만" if cluster.paper_count == 1 else ""
        blocks.append(
            f"[분야 {cluster.cluster_id}] 논문 {cluster.paper_count}편{single}{span}\n"
            f"  키워드: {', '.join(cluster.topic_keywords) or '없음'}\n"
            "  논문 제목(연도순):\n" + "\n".join(f"  - {t}" for t in titles or ["제목 없음"])
        )
    return f"다음 연구자의 논문 {total}편을 {len(clusters)}개 분야로 묶은 결과다.\n\n" + "\n\n".join(blocks)


def _max_tokens(cluster_count: int) -> int:
    """분야 수에 상한이 없어 출력 길이도 분야 수에 비례한다. 분야 하나에 분야명+설명 약 120토큰."""
    return min(_MAX_TOKENS_CAP, _MAX_TOKENS_BASE + 120 * cluster_count)


def _parse_llm(raw: str) -> tuple[dict[int, str], dict[int, str], Optional[str]]:
    """분야명, 분야 설명, 전체 요약. 일부 분야가 빠져 있어도 받은 만큼은 쓴다."""
    body = raw.strip()
    if body.startswith("```"):
        body = body.split("```")[1] if "```" in body[3:] else body.strip("`")
        body = body.removeprefix("json").strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("JSON 객체를 찾지 못함")
    data = json.loads(body[start : end + 1])
    topics: dict[int, str] = {}
    descriptions: dict[int, str] = {}
    for key, value in (data.get("clusters") or {}).items():
        cluster_id = _cluster_id_from_key(key)
        if cluster_id is None:
            continue
        if isinstance(value, dict):
            if value.get("topic"):
                topics[cluster_id] = str(value["topic"]).strip()
            if value.get("description"):
                descriptions[cluster_id] = str(value["description"]).strip()
        elif value:  # 모델이 분야명만 문자열로 돌려준 경우
            topics[cluster_id] = str(value).strip()
    summary = (data.get("summary") or "").strip() or None
    return topics, descriptions, summary


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
    clusters: list[ResearchFlowCluster],
) -> tuple[dict[int, str], dict[int, str], Optional[str], str, Optional[str]]:
    """LLM에게 분야명·분야 설명·전체 요약만 맡긴다. 실패하면 규칙 기반으로 폴백한다 —
    명세가 요약 카드를 '상시 노출'로 요구하므로 문장이 없다고 화면을 비울 수는 없다."""
    from app.services.llm.client import LLMBudgetExceededError, LLMRefusalError, chat

    try:
        response = await chat(
            messages=[{"role": "user", "content": _user_prompt(clusters)}],
            model=_MODEL,
            system=_system_prompt(),
            temperature=0.3,
            max_tokens=_max_tokens(len(clusters)),
        )
        topics, descriptions, summary = _parse_llm(response.text)
        return topics, descriptions, summary, "llm", response.model
    except LLMBudgetExceededError:
        logger.warning("연구 흐름 요약: 이번 달 LLM 예산 소진 — 규칙 기반으로 폴백")
    except LLMRefusalError:
        logger.warning("연구 흐름 요약: 모델이 응답을 거부 — 규칙 기반으로 폴백")
    except Exception as exc:  # 파싱 실패·네트워크 오류 등
        logger.warning("연구 흐름 요약 생성 실패(%s) — 규칙 기반으로 폴백", type(exc).__name__)
    return {}, {}, None, "rule", None


def _order_labels(rows: list[Any], labels: np.ndarray) -> np.ndarray:
    """묶음 번호를 화면 순서로 다시 매긴다 — 0번이 가장 최근까지 이어진 분야.

    기획 명세: "각 카드에서 가장 마지막으로 한 연구가 최근인 순" (2012~2026이 2021~2024보다 앞).
    끝 연도가 같으면 그 카드의 마지막 논문이 더 최근인 쪽(같은 해 안의 발행월), 그래도 같으면
    논문이 많은 쪽을 앞에 둔다. rows가 과거 → 최신 정렬이라 인덱스가 클수록 최근 논문이다.
    연도를 모르는 논문뿐인 카드는 맨 뒤. cluster_id가 곧 순서라 오른쪽 목록과 왼쪽 카드가 같은 번호를 가리킨다.
    """
    groups: dict[int, list[int]] = {}
    for i, label in enumerate(labels.tolist()):
        groups.setdefault(label, []).append(i)

    def key(label: int) -> tuple:
        indices = groups[label]
        dated = [i for i in indices if rows[i].pubyear]
        if not dated:
            return (1, 0, 0, -len(indices), indices[0])
        last = max(dated)
        return (0, -rows[last].pubyear, -last, -len(indices), indices[0])

    remap = {old: new for new, old in enumerate(sorted(groups, key=key))}
    return np.array([remap[label] for label in labels.tolist()], dtype=int)


def _build_clusters(rows: list[Any], labels: np.ndarray) -> list[ResearchFlowCluster]:
    """분야 카드.

    rows는 과거 → 최신으로 정렬돼 들어오고 labels는 _order_labels를 거친 값이라,
    번호 순으로 돌기만 하면 카드 순서와 카드 안 논문 순서가 모두 연도 오름차순이 된다.
    """
    clusters: list[ResearchFlowCluster] = []
    for label in sorted(set(labels.tolist())):
        indices = [i for i in range(len(rows)) if labels[i] == label]
        keywords = _cluster_keywords(rows, indices)
        years = [rows[i].pubyear for i in indices if rows[i].pubyear]
        clusters.append(
            ResearchFlowCluster(
                cluster_id=int(label),
                topic=_rule_topic(keywords),
                topic_keywords=keywords,
                paper_count=len(indices),
                start_year=min(years, default=None),
                end_year=max(years, default=None),
                papers=[_flow_paper(rows[i]) for i in indices],
            )
        )
    return clusters


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
    """논문 수와 무관하게 항상 응답한다(명세: 요약 카드 상시 노출)."""
    rows = sorted(await fetch_paper_rows(db, researcher_id), key=_order_key)
    if not rows:
        return ResearchFlowResponse(
            researcher_id=researcher_id,
            total_papers=0,
            flow_level="none",
            summary=None,
            summary_source="none",
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
    labels = _order_labels(rows, await asyncio.to_thread(_embed_and_cluster, rows))
    clusters = _build_clusters(rows, labels)

    topics, descriptions, summary, source, model = await _write_sentences(clusters)
    for cluster in clusters:
        if cluster.cluster_id in topics:
            cluster.topic = topics[cluster.cluster_id]
        # 1편짜리 분야는 설명 없이 논문만 보여준다(기획 확정). 모델이 써 와도 버린다.
        cluster.description = descriptions.get(cluster.cluster_id) if cluster.paper_count > 1 else None
    if summary is not None and len(summary) > _SUMMARY_MAX_CHARS:
        logger.info("연구 흐름 요약이 %d자라 규칙 기반으로 대체", len(summary))
        summary = None
    if summary is None:
        summary = _rule_summary(clusters)
        source = "rule" if source == "llm" else source

    result = ResearchFlowResponse(
        researcher_id=researcher_id,
        total_papers=len(rows),
        flow_level=_flow_level(len(rows), len(clusters)),
        summary=summary,
        summary_source=source,
        clusters=clusters,
    )
    try:
        await _save_cache(db, researcher_id, signature, result, model)
    except Exception:
        logger.exception("연구 흐름 캐시 저장 실패 — 응답에는 영향 없음")
        await db.rollback()
    return result
