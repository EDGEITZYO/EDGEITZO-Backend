from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from app.models.base import Base


class PaperSelectionReason(Base):
    """논문 선정 사유 영속 캐시 (명세 02-11).

    초록은 논문에 붙은 고정값이라 꺼내 쓰면 그만이지만, 선정 사유는 "이 논문 + 이 검색어"
    조합마다 달라지는 값이라 미리 만들어둘 수 없다. 그래서 최초 조회 시 생성해 저장하고
    이후 같은 조합은 재사용한다 — keyword_definitions와 같은 패턴.

    필터/정렬 변경은 keyword_key를 바꾸지 않으므로(결과를 좁히거나 재배열할 뿐),
    정렬을 바꿔 새 논문이 상위로 올라와도 이미 만든 사유는 그대로 재사용된다.
    """

    __tablename__ = "paper_selection_reasons"

    paper_id = Column(String(100), primary_key=True)
    # 검색 키워드를 정규화·정렬해 이어붙인 값 (selection_reason_service.build_keyword_key)
    keyword_key = Column(String(500), primary_key=True)
    # 프롬프트 규칙이 바뀌면 기존 행은 구 버전 글이 된다 — 키에 넣어 자동 재생성되게 한다
    prompt_version = Column(String(20), primary_key=True)

    reason = Column(Text, nullable=False)
    # 강조 구절의 reason 내 위치. 마커 파싱에 실패하면 null이고, 그때는 본문만 그린다.
    highlight_start = Column(Integer, nullable=True)
    highlight_end = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=False)  # 공백 포함 (명세 기준과 동일)
    model = Column(String(50), nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_paper_selection_reasons_version", "prompt_version"),
    )
