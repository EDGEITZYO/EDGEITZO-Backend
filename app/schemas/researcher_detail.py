"""연구자 상세페이지 응답 스키마 (명세 08-01 ~ 08-06).

좌표·색상·노드 크기·제목 축약은 전부 프런트가 정한다. 백엔드는 값과 관계만 준다
(연구자 탐색의 field-graph와 같은 원칙).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


PaperSortKey = Literal["recent", "citations"]


# ---------------------------------------------------------------- 08-01 프로필

class ResearcherProfileResponse(BaseModel):
    """상세페이지 상단 핵심 정보.

    미확보 값은 전부 null로 내려간다. 화면의 '데이터 없음'·공란('-')은 프런트가 그린다
    — 백엔드가 '데이터 없음' 문자열을 내려보내면 숫자 필드의 타입이 깨진다.
    """

    researcher_id: str = Field(description="연구자 고유 ID", example="kci:c33f2b39da0f45a6")
    name_kor: Optional[str] = Field(None, description="한글명. OpenAlex 출처 연구자는 없을 수 있음", example="이기한")
    name_eng: Optional[str] = Field(None, description="영문명. 없으면 null", example="Lee, Ki-Han")
    institution: Optional[str] = Field(None, description="현재 소속 기관. 없으면 null", example="서울여자대학교")
    department: Optional[str] = Field(
        None,
        description="전공(학과). 적재 커버리지 17%로 대부분 null — 화면에서 '전공: ' 라벨째 숨길 것",
        example="화학과",
    )
    keywords: list[str] = Field(default_factory=list, description="대표 연구 분야 키워드. 칩 태그로 표시")
    email: Optional[str] = Field(
        None,
        description=(
            "이메일. 출처 신뢰도가 confirmed/domain_verified인 경우에만 내려간다. "
            "추정(inferred) 건은 남의 연락처가 잘못 노출될 수 있어 null 처리 — 화면은 '-'"
        ),
        example="lee@swu.ac.kr",
    )
    total_papers: Optional[int] = Field(None, description="총 논문 수", example=24)
    total_citations: Optional[int] = Field(None, description="총 피인용 수. 미집계면 null → 화면 '데이터 없음'", example=480)
    citation_source: Optional[str] = Field(
        None,
        description="피인용 집계 출처. 'kci'(국내 등재지) | 'openalex'(국제). 집계 범위가 달라 두 출처의 수치를 한 척도로 비교하면 안 됨",
        example="kci",
    )
    corpus_paper_count: int = Field(0, description="우리 서비스 코퍼스에 있는 논문 수", example=2)
    first_pubyear: Optional[int] = Field(None, description="첫 논문 발행연도", example=2006)
    last_pubyear: Optional[int] = Field(None, description="최근 논문 발행연도", example=2025)


# ------------------------------------------------------- 08-02 / 08-03 논문 리스트

class ResearcherPaperItem(BaseModel):
    """논문 카드. 기존 논문 카드 컴포넌트를 그대로 쓰도록 필드명을 맞췄다.

    연구자 논문의 93.6%는 코퍼스 밖(KCI 이력)이라 우리 상세페이지가 없다.
    is_internal이 false면 external_url(KCI 원문)로 보내야 하고, 북마크·읽음도 불가능하다.
    """

    paper_id: Optional[str] = Field(None, description="우리 DB 논문 ID. 코퍼스 밖이면 null", example="JAKO202509339655899")
    external_id: Optional[str] = Field(
        None,
        description="KCI 논문 ID. 코퍼스에만 있고 KCI 식별자가 없는 논문(372편)은 null",
        example="ART002780520",
    )
    title: Optional[str] = Field(None, description="논문 제목")
    journal_name: Optional[str] = Field(None, description="학술지명", example="한국정보과학회 논문지")
    pub_year: Optional[int] = Field(None, description="발행연도", example=2024)
    pub_month: Optional[str] = Field(None, description="발행월 2자리. 없으면 null", example="06")
    published_at: Optional[str] = Field(
        None,
        description="표시용 발행일. 'YYYY-MM-DD' 또는 'YYYY-MM'. KCI가 일자를 주지 않는 건은 월까지만",
        example="2024-06-30",
    )
    authors: list[str] = Field(default_factory=list, description="저자 목록. 화면의 '홍길동 외 N인'은 이 배열로 조립")
    abstract: Optional[str] = Field(
        None,
        description="초록. 코퍼스 밖 논문은 KCI 초록 적재 전까지 null — 카드에서 초록 영역을 접을 것",
    )
    keywords: list[str] = Field(default_factory=list, description="논문 키워드. 칩 태그")
    citation_count: Optional[int] = Field(
        None,
        description="인용 수. KCI 미집계분(34%)은 null — 0과 구분되며 배지를 숨겨야 함",
        example=110,
    )
    paper_type: Optional[str] = Field(None, description="논문 유형. '학술 저널' | '박사학위 논문' | '석사학위 논문' | null")
    kci_registered: bool = Field(False, description="KCI 등재 여부. KCI 배지")
    sci_indexed: Optional[bool] = Field(
        None,
        description="SCI 계열 등재 여부. 학술지명 매칭 실패(9%) 시 null → 배지 숨김. false는 '비SCI 확정'",
    )
    doi: Optional[str] = Field(None, description="DOI. 없으면 null")
    external_url: Optional[str] = Field(None, description="KCI 원문 링크. 코퍼스 밖 논문의 이동 대상")
    is_internal: bool = Field(description="우리 논문 상세페이지로 이동 가능한지")
    can_bookmark: bool = Field(description="북마크 가능 여부. is_internal과 같은 조건이지만 화면 의미가 달라 분리")
    is_bookmarked: bool = Field(False, description="요청자의 북마크 여부. 비로그인 시 항상 false")
    read_at: Optional[str] = Field(None, description="요청자가 읽은 시각(ISO8601). 안 읽었거나 비로그인이면 null")
    role: Optional[str] = Field(None, description="저자 역할. '제1' | '교신' | '참여' | '단독' | null")
    author_order: Optional[int] = Field(None, description="저자 순서. 없으면 null")


class ResearcherPaperListResponse(BaseModel):
    """08-02 단순 리스트."""

    researcher_id: str
    total: int = Field(description="필터 적용 후 전체 논문 수")
    page: int
    size: int
    sort: PaperSortKey = Field(description="적용된 정렬")
    citation_sort_available: bool = Field(
        description=(
            "피인용순 정렬을 드롭다운에 노출해도 되는지. 명세 08-02의 '피인용 데이터가 충분한 경우에 한해' 조건. "
            "인용수가 있는 논문이 3편 미만이면 false"
        )
    )
    items: list[ResearcherPaperItem]


class ResearcherPaperYearGroup(BaseModel):
    year: Optional[int] = Field(description="발행연도. 연도 미상 논문은 null 그룹으로 맨 뒤")
    count: int
    items: list[ResearcherPaperItem]


class ResearcherPaperYearListResponse(BaseModel):
    """08-03 연도별 논문 이력. 08-02와 같은 데이터를 연도로 묶기만 한다."""

    researcher_id: str
    total: int
    page: int
    size: int
    groups: list[ResearcherPaperYearGroup] = Field(description="연도 내림차순(최신 연도 상단)")


# ------------------------------------------------------------- 08-04 공저자

class CoauthorItem(BaseModel):
    """함께 연구한 사람. 호버 팝오버가 추가 요청 없이 그려지도록 팝오버 내용을 전부 포함한다."""

    researcher_id: str = Field(description="공저자 연구자 ID. 팝오버 '상세 정보' 버튼의 이동 대상")
    name: Optional[str] = Field(None, description="이름", example="박지후")
    institution: Optional[str] = Field(None, description="소속 대학", example="한국대학교")
    department: Optional[str] = Field(None, description="학과. 없으면 null", example="생명공학과")
    keywords: list[str] = Field(default_factory=list, description="연구 키워드. 팝오버에 표시")
    co_paper_count: int = Field(description="함께 쓴 논문 수. 팝오버 '함께 쓴 N편 보기'의 N", example=5)


class CoauthorListResponse(BaseModel):
    researcher_id: str
    total: int = Field(description="공저자 총원. 0이면 화면에 '0명'")
    items: list[CoauthorItem] = Field(description="함께 쓴 논문 수 내림차순")


# ------------------------------------------------- 08-05 / 08-06 연구 흐름

class ResearchFlowNode(BaseModel):
    """논문 1편 = 노드 1개. X축 배치는 pub_year/pub_month로 프런트가 계산한다."""

    node_id: str = Field(description="그래프 내 노드 키", example="ART002780520")
    paper_id: Optional[str] = Field(None, description="우리 DB 논문 ID. 없으면 null")
    external_id: Optional[str] = None
    title: Optional[str] = Field(None, description="논문 제목 전체. 축약은 프런트가 함")
    authors: list[str] = Field(default_factory=list, description="노드 라벨의 '홍길동 외 4인'용")
    pub_year: Optional[int] = None
    pub_month: Optional[str] = None
    published_at: Optional[str] = None
    cluster_id: Optional[int] = Field(None, description="속한 연구 주제 클러스터. 어디에도 안 묶이면 null(흰색 노드)")
    is_core: bool = Field(False, description="클러스터 대표 논문인지. 화면의 진한 초록")
    is_internal: bool = Field(description="논문 상세로 이동 가능한지")
    external_url: Optional[str] = None
    citation_count: Optional[int] = None


class ResearchFlowEdge(BaseModel):
    """관련 있는 논문끼리의 연결. 방향은 항상 과거 → 최신."""

    source: str = Field(description="과거 쪽 node_id")
    target: str = Field(description="최신 쪽 node_id")
    weight: float = Field(description="0~1 관련도. 선 굵기/투명도에 쓸 값", example=0.78)
    shared_keywords: list[str] = Field(
        default_factory=list,
        description="두 논문이 실제로 공유하는 키워드. 비어 있어도 의미 유사도로 이어진 것이라 정상",
    )


class ResearchFlowClusterPaper(BaseModel):
    node_id: str
    paper_id: Optional[str] = None
    title: Optional[str] = None
    year: Optional[int] = None


class ResearchFlowCluster(BaseModel):
    """연구 흐름 요약 카드 1장 (08-06)."""

    cluster_id: int
    topic: str = Field(
        description="클러스터 대표 주제명. LLM이 실제 논문 키워드만 써서 문장화한 결과",
        example="오가노이드 기반 재생의학 및 약물 독성 평가 연구",
    )
    topic_keywords: list[str] = Field(
        default_factory=list,
        description="주제명의 근거가 된 실제 논문 키워드. LLM이 없는 주제를 지어냈는지 프런트/QA가 대조할 수 있게 함",
    )
    paper_count: int
    start_paper: Optional[ResearchFlowClusterPaper] = Field(None, description="이 흐름의 시작 논문")
    latest_paper: Optional[ResearchFlowClusterPaper] = Field(None, description="가장 최근 논문. 논문이 1편뿐이면 null")
    has_followup: bool = Field(description="후속 연구가 있는지. false면 화면에 '후속 연구 없음'")
    node_ids: list[str] = Field(description="카드 클릭 시 하이라이트할 노드들")


class ResearchFlowResponse(BaseModel):
    """08-05 + 08-06. 논문 수와 무관하게 항상 200으로 응답한다(명세: 상시 노출)."""

    researcher_id: str
    total_papers: int = Field(description="그래프에 그린 논문 수")
    summary: Optional[str] = Field(
        None,
        description="연구 흐름 요약 1문장. 논문이 없거나 LLM 생성에 실패하면 null",
        example="2006년 효소 분해 연구에서 출발해 최근에는 염료감응 태양전지로 관심이 옮겨갔습니다.",
    )
    summary_source: Literal["llm", "rule", "none"] = Field(
        description="요약 문장을 무엇이 만들었는지. LLM 예산 소진·거부 시 rule로 폴백"
    )
    nodes: list[ResearchFlowNode]
    edges: list[ResearchFlowEdge]
    clusters: list[ResearchFlowCluster] = Field(description="요약 카드 목록. 논문 수 내림차순")
