from datetime import datetime, timezone

from sqlalchemy import REAL, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY

from app.models.base import Base


class Researcher(Base):
    """연구자 1급 엔티티.

    식별자는 출처 접두어가 붙는다 — ScienceON의 연구자↔논문 색인이 불완전해(실측 매칭률 46%)
    KCI articleDetail 파생 식별자를 기본으로 쓰고, ScienceON CN은 속성으로 내려왔다.

        kci:<sha1(정규화이름|기관루트)[:16]>   KCI 파생 (국내 학술지 저자)
        oa:<openalex author id>              OpenAlex (해외 학술지 저자)
        sci:<scienceon cn>                   ScienceON에서만 확인된 연구자
    """

    __tablename__ = "researchers"

    researcher_id = Column(String(100), primary_key=True)
    source = Column(String(20), nullable=False)  # kci | openalex | scienceon
    scienceon_cn = Column(String(100), nullable=True, index=True)

    author_name_kor = Column(String(300), nullable=True, index=True)
    author_name_eng = Column(String(300), nullable=True)
    author_inst_kor = Column(String(500), nullable=True)
    author_inst_eng = Column(String(500), nullable=True)
    rno = Column(String(100), nullable=True)

    institution_current = Column(String(500), nullable=True, index=True)
    institution_dept = Column(String(500), nullable=True)
    institution_history = Column(ARRAY(String(500)), nullable=True)

    email = Column(String(200), nullable=True)
    # 이메일 출처 신뢰도. 'confirmed' = 논문 CN 교집합까지 확인, 'inferred' = 이름+소속 유일 일치.
    # 남의 이메일을 프로필에 띄우는 값이라, 화면에 노출할 등급은 API 계층에서 정한다.
    match_confidence = Column(String(20), nullable=True)

    keyword = Column(ARRAY(String(200)), nullable=True)  # ScienceON 원본 (레거시)
    keywords = Column(ARRAY(String(200)), nullable=True)  # 대표 연구 분야

    article_cnt = Column(Integer, nullable=True)
    patent_cnt = Column(Integer, nullable=True)
    report_cnt = Column(Integer, nullable=True)

    total_papers = Column(Integer, nullable=True)
    total_citations = Column(Integer, nullable=True)
    citation_source = Column(String(20), nullable=True)  # kci | openalex
    corpus_paper_count = Column(Integer, default=0, nullable=False)
    first_pubyear = Column(Integer, nullable=True)
    last_pubyear = Column(Integer, nullable=True)
    papers_truncated = Column(Boolean, default=False, nullable=False)
    expanded_at = Column(DateTime(timezone=True), nullable=True)

    # 연구 분야 유사도용 벡터. 명세서의 "키워드 공유" 방식은 분야 검색 결과 안에서
    # 쌍의 45%가 공유 0이라 그래프 절반이 그려지지 않는다(실측).
    embedding = Column(ARRAY(REAL), nullable=True)          # 내용 기반 (BGE-m3-ko)
    embedding_graph = Column(ARRAY(REAL), nullable=True)    # 공저 네트워크 반영 (GraphSAGE)
    embedding_model = Column(String(100), nullable=True)
    embedding_text = Column(Text, nullable=True)            # 어떤 문장을 임베딩했는지 (디버깅용)
    embedded_at = Column(DateTime(timezone=True), nullable=True)

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
    """연구자-논문 다대다 조인. Paper.authors(표시 전용 문자열 배열)와 달리
    Researcher는 자체 PK/속성을 가진 1급 엔티티라 실제 JOIN이 필요."""

    __tablename__ = "researcher_papers"

    researcher_id = Column(
        String(100), ForeignKey("researchers.researcher_id", ondelete="CASCADE"), primary_key=True
    )
    paper_id = Column(String(100), ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    author_order = Column(Integer, nullable=True)
    role = Column(String(20), nullable=True)  # 제1 | 교신 | 참여 | 단독
    institution_at_time = Column(String(500), nullable=True)


class ResearcherExternalPaper(Base):
    """연구자의 코퍼스 밖 논문. 저자 85%가 코퍼스에는 논문 1편뿐이라,
    「총 논문 수」「연도별 이력」「연구 흐름」은 이 테이블 없이는 성립하지 않는다.
    (paper_citation_external_refs와 같은 패턴 — 표시용 메타데이터만, 상세페이지 없음)"""

    __tablename__ = "researcher_external_papers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    researcher_id = Column(
        String(100), ForeignKey("researchers.researcher_id", ondelete="CASCADE"), nullable=False
    )
    external_source = Column(String(20), nullable=False)  # kci | openalex
    external_id = Column(String(100), nullable=False)
    title = Column(String(1000), nullable=True)
    journal = Column(String(500), nullable=True)
    pubyear = Column(Integer, nullable=True)
    pubmonth = Column(String(2), nullable=True)
    citation_count = Column(Integer, default=0, nullable=False)
    categories = Column(ARRAY(String(200)), nullable=True)
    keywords = Column(ARRAY(String(200)), nullable=True)  # keywords 단계에서 채움
    # 그 논문의 전체 저자. 「함께 연구한 사람들」은 코퍼스 논문만으로는 10%밖에 못 채운다
    # (인만진: 코퍼스 9편 vs 실제 90편) — 외부 논문 저자까지 봐야 온전해진다.
    authors = Column(ARRAY(String(300)), nullable=True)
    author_institutions = Column(ARRAY(String(500)), nullable=True)
    doi = Column(String(200), nullable=True)
    url = Column(String(1000), nullable=True)
    internal_paper_id = Column(String(100), ForeignKey("papers.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        Index("ix_researcher_external_papers_rid_year", "researcher_id", "pubyear"),
    )
