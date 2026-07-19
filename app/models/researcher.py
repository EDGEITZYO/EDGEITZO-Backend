from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY

from app.models.base import Base


class Researcher(Base):
    """ScienceON 연구자 검색/상세보기 API 응답 필드에 그대로 매핑. 데이터 적재는 별도 배치(미작성)에서 채움."""

    __tablename__ = "researchers"

    cn = Column(String(100), primary_key=True)  # ScienceON 연구자 CN
    author_name_kor = Column(String(300), nullable=True)
    author_name_eng = Column(String(300), nullable=True)
    author_inst_kor = Column(String(500), nullable=True)
    author_inst_eng = Column(String(500), nullable=True)
    rno = Column(String(100), nullable=True)  # 원시제어번호
    email = Column(String(200), nullable=True)
    keyword = Column(ARRAY(String(200)), nullable=True)  # Paper.keywords_ko와 동일 컨벤션
    article_cnt = Column(Integer, nullable=True)
    patent_cnt = Column(Integer, nullable=True)
    report_cnt = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ResearcherPaper(Base):
    """연구자-논문 다대다 조인 테이블. Paper.authors(표시 전용 문자열 배열)와 달리
    Researcher는 자체 PK/속성을 가진 1급 엔티티라 실제 JOIN이 필요."""

    __tablename__ = "researcher_papers"

    researcher_cn = Column(String(100), ForeignKey("researchers.cn", ondelete="CASCADE"), primary_key=True)
    paper_id = Column(String(100), ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
