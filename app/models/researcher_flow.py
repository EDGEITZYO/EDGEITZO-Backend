from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base


class ResearcherFlowCache(Base):
    """연구 흐름 그래프 + 요약 카드 영속 캐시 (명세 08-05 / 08-06).

    paper_selection_reasons와 같은 패턴이다. 다만 캐시하는 대상이 다르다 —
    여기서는 LLM 문장뿐 아니라 **클러스터 배정과 엣지까지** 통째로 담는다.
    요약 카드의 주제명이 가리키는 노드가 다음 요청에서 다른 묶음이 되면 화면이 어긋나기
    때문에, 문장과 묶음은 같은 시점에 만들어진 한 벌로 유지해야 한다.

    무효화는 두 가지로 한다:
      prompt_version   프롬프트·클러스터링 규칙이 바뀌면 키가 바뀌어 자동 재생성
      paper_signature  그 연구자의 논문 목록이 바뀌면(신규 적재 등) 값이 달라져 재생성
    """

    __tablename__ = "researcher_flow_cache"

    researcher_id = Column(String(100), primary_key=True)
    prompt_version = Column(String(20), primary_key=True)

    # 논문 키 목록의 sha1. 논문이 늘거나 줄면 달라진다.
    paper_signature = Column(String(64), nullable=False)
    # ResearchFlowResponse에서 researcher_id를 뺀 본문 (nodes/edges/clusters/summary)
    payload = Column(JSONB, nullable=False)
    # 'llm' | 'rule' — 요약 문장을 무엇이 만들었는지. rule이면 예산 복구 후 재생성 대상이 된다.
    summary_source = Column(String(10), nullable=False)
    model = Column(String(50), nullable=True)
    paper_count = Column(Integer, nullable=False, default=0)

    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
