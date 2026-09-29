"""연구자 상세페이지 응답 스키마 (명세 08-01 ~ 08-06).

백엔드는 값과 관계만 계산해 내보낸다 — 연구자 탐색 field-graph와 같은 원칙이다.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.paper import PaperCardTrustBadge


PaperSortKey = Literal["recent", "citations"]


# ---------------------------------------------------------------- 08-01 프로필

class ResearcherProfileResponse(BaseModel):
    """연구자 핵심 정보.

    미확보 값은 전부 null로 내려간다. '데이터 없음' 같은 문자열을 숫자 필드에 넣으면
    타입이 깨지므로 표기는 소비자 쪽 몫으로 남긴다.
    """

    researcher_id: str = Field(description="연구자 고유 ID", example="kci:c33f2b39da0f45a6")
    name_kor: Optional[str] = Field(None, description="한글명. OpenAlex 출처 연구자는 없을 수 있음", example="이기한")
    name_eng: Optional[str] = Field(None, description="영문명. 없으면 null", example="Lee, Ki-Han")
    institution: Optional[str] = Field(None, description="현재 소속 기관. 없으면 null", example="서울여자대학교")
    department: Optional[str] = Field(
        None,
        description="전공(학과). 연구자의 56%가 보유(2026-09-30, 논문 속 본인 소속에서 뽑은 값 포함). 없으면 null",
        example="화학과",
    )
    keywords: list[str] = Field(default_factory=list, description="대표 연구 분야 키워드")
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
    """논문 한 편. 필드명은 기존 논문 카드 응답과 맞췄다.

    연구자 논문의 93.6%는 코퍼스 밖(KCI 이력)이다. is_internal이 false면 papers 행이 없어
    우리 논문 상세·북마크·읽음이 성립하지 않고, 이동 대상은 external_url뿐이다.
    """

    paper_id: Optional[str] = Field(
        None,
        description=(
            "논문 상세·북마크에 넣을 ID(기존 논문 카드 PaperCardResponse.paper_id와 같은 역할). "
            "우리 DB 논문 ID가 있으면 그 값, 없으면 KCI 논문 ID(ART…) — 상세·북마크 API가 KCI ID를 받으면 그 자리에서 적재한다. "
            "둘 다 없으면 null. detail_id와 같은 값이다"
        ),
        example="JAKO202509339655899",
    )
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
        description="초록. papers에 편입되지 않은 논문은 null (KCI articleDetail 적재 시 채워짐)",
    )
    keywords: list[str] = Field(default_factory=list, description="논문 키워드")
    citation_count: Optional[int] = Field(
        None,
        description="인용 수. 0이면 0, 값이 없을 때만 null(기존 논문 리스트와 같은 규칙)",
        example=110,
    )
    paper_type: Optional[str] = Field(None, description="논문 유형. '학술 저널' | '박사학위 논문' | '석사학위 논문' | null")
    kci_registered: bool = Field(False, description="KCI 등재 여부")
    sci_indexed: Optional[bool] = Field(
        None,
        description=(
            "SCI 계열 등재 여부. journals 조인 결과로, 학술지명 매칭 실패(9%) 시 null. "
            "false는 '비SCI 확정'이라 null과 의미가 다르다. papers 편입 여부와 무관하게 채워진다"
        ),
    )
    doi: Optional[str] = Field(None, description="DOI. 없으면 null")
    trust_badge: Optional[PaperCardTrustBadge] = Field(
        None, description="신뢰도 뱃지 — 기존 논문 카드와 같은 구조(kci / sci / citation_count / degree_type)"
    )
    external_url: Optional[str] = Field(None, description="KCI 원문 링크")
    is_internal: bool = Field(description="papers에 행이 이미 있는지")
    detail_id: Optional[str] = Field(
        None,
        description=(
            "논문 상세(GET /papers/{paper_id})·북마크에 넣을 ID. paper_id가 있으면 그 값, 없으면 KCI 논문 ID(ART…). "
            "KCI ID면 상세 API가 첫 조회 때 KCI에서 받아 적재한다. 둘 다 없으면 null"
        ),
        example="ART002780520",
    )
    can_open_detail: bool = Field(False, description="detail_id가 있는지 — 논문 상세로 이동 가능한지")
    can_bookmark: bool = Field(
        description="북마크 가능 여부. detail_id가 있으면 true — 북마크 API도 KCI ID면 논문을 적재한 뒤 저장한다"
    )
    is_bookmarked: bool = Field(False, description="요청자의 북마크 여부. 비로그인 시 false")
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
            "피인용순 정렬이 의미 있는지. 명세 08-02의 '피인용 데이터가 충분한 경우에 한해' 조건으로, "
            "인용수가 있는 논문이 3편 미만이면 false이며 sort=citations 요청은 recent로 되돌아간다"
        )
    )
    items: list[ResearcherPaperItem]


class ResearcherPaperYearGroup(BaseModel):
    year: Optional[int] = Field(description="발행연도. 연도 미상 논문은 null 그룹")
    count: int
    items: list[ResearcherPaperItem]


class ResearcherPaperYearListResponse(BaseModel):
    """08-03 연도별 논문 이력. 08-02와 같은 조회 결과를 연도로 묶은 것이라 두 응답이 어긋나지 않는다."""

    researcher_id: str
    total: int
    page: int
    size: int
    groups: list[ResearcherPaperYearGroup] = Field(description="연도 내림차순(최신 연도 상단)")


# ------------------------------------------------------------- 08-04 공저자

class CoauthorItem(BaseModel):
    """함께 연구한 사람. 명세 08-04가 요구하는 항목(이름·소속·전공·함께 쓴 논문 수)을
    목록 응답에 모두 담아 추가 조회가 필요 없게 했다."""

    researcher_id: str = Field(description="공저자 연구자 ID. 이 값으로 다시 상세 조회가 가능하다")
    name: Optional[str] = Field(None, description="이름")
    institution: Optional[str] = Field(None, description="소속 기관")
    department: Optional[str] = Field(
        None,
        description="전공(학과). 연구자의 56%가 보유(2026-09-30). 논문 속 본인 소속 문자열에서 뽑은 값을 포함한다. 없으면 null",
    )
    department_display: Optional[str] = Field(
        None,
        description="전공이 있으면 전공, 없으면 소속 기관. 둘 다 없으면 null",
        example="환경공학과",
    )
    department_source: Optional[Literal["department", "institution"]] = Field(
        None, description="department_display가 전공(department)인지 소속(institution)인지. 둘 다 없으면 null"
    )
    keywords: list[str] = Field(default_factory=list, description="연구 키워드")
    co_paper_count: int = Field(description="함께 쓴 논문 수. papers?coauthor_id= 필터 결과 건수와 같다", example=5)


class CoauthorListResponse(BaseModel):
    researcher_id: str
    total: int = Field(description="공저자 총원")
    items: list[CoauthorItem] = Field(description="함께 쓴 논문 수 내림차순")


# ------------------------------------------------- 08-05 / 08-06 연구 흐름

class ResearchFlowPaper(BaseModel):
    """분야에 속한 논문 한 편. 08-02 논문 리스트 항목과 같은 이름·의미의 필드만 추렸다."""

    node_id: str = Field(description="논문 키. paper_id가 있으면 그 값, 없으면 external_id라 항상 채워진다")
    paper_id: Optional[str] = Field(None, description="우리 DB 논문 ID. 코퍼스 밖이면 null")
    external_id: Optional[str] = Field(None, description="KCI 논문 ID")
    title: Optional[str] = Field(None, description="논문 제목")
    journal_name: Optional[str] = Field(None, description="학술지명. 없으면 null")
    citation_count: Optional[int] = Field(
        None, description="인용 수. 0이면 0, 값을 못 불러온 논문만 null이다"
    )
    pub_year: Optional[int] = Field(None, description="발행연도")
    published_at: Optional[str] = Field(
        None, description="발행일. 'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY' — 출처가 준 정밀도까지만"
    )
    is_internal: bool = Field(description="papers에 행이 이미 있는지")
    detail_id: Optional[str] = Field(
        None, description="논문 상세에 넣을 ID. paper_id가 있으면 그 값, 없으면 KCI 논문 ID(ART…). 둘 다 없으면 null"
    )
    external_url: Optional[str] = Field(None, description="KCI 원문 링크")


class ResearchFlowCluster(BaseModel):
    """의미가 가까운 논문끼리 묶은 분야 하나."""

    cluster_id: int
    topic: str = Field(
        description="분야명. 아래 topic_keywords와 논문 제목만 근거로 LLM이 문장화한 결과",
        example="오가노이드 기반 재생의학 및 약물 독성 평가 연구",
    )
    description: Optional[str] = Field(
        None,
        description=(
            "이 분야 안에서 연구가 시간에 따라 어떻게 흘러왔는지 한 문장(LLM). "
            "'~ 연구로 시작해 ~로 변화한 흐름을 보여요' / '~에 관련한 연구를 지속해서 진행하고 있어요' 형태. "
            "논문 제목·키워드에 있는 주제만 쓴다. **논문이 1편인 분야는 항상 null**(분야 제목과 논문만 제공). "
            "생성에 실패해도 null"
        ),
    )
    topic_keywords: list[str] = Field(
        default_factory=list,
        description="분야명의 근거가 된 실제 논문 키워드. 지어낸 주제가 섞였는지 대조할 수 있게 함께 내보낸다",
    )
    paper_count: int = Field(description="이 분야에 속한 논문 수")
    start_year: Optional[int] = Field(None, description="이 분야 논문 중 가장 이른 발행연도. 연도 미상뿐이면 null")
    end_year: Optional[int] = Field(None, description="이 분야 논문 중 가장 늦은 발행연도")
    papers: list[ResearchFlowPaper] = Field(
        default_factory=list, description="이 분야의 논문 전부. 발행연도 오름차순(과거 → 최신), 연도 미상은 끝"
    )


class ResearchFlowResponse(BaseModel):
    """08-05 + 08-06. 논문이 0편이어도 200으로 응답한다(명세: 논문 수와 무관하게 상시 제공)."""

    researcher_id: str
    total_papers: int = Field(description="분야로 나눈 논문 수. 모든 분야의 paper_count 합과 같다")
    flow_level: Literal["none", "single", "flow"] = Field(
        description=(
            "이 응답에 분야가 몇 개로 나뉘었는지.\n"
            "- `none`: 논문이 0~1편이라 묶을 것이 없다\n"
            "- `single`: 논문이 2편 이상이지만 전부 한 분야로 묶였다\n"
            "- `flow`: 분야가 2개 이상이다"
        )
    )
    summary: Optional[str] = Field(
        None,
        description="연구자 전체 논문에 대한 한 줄 요약, 100자 이하. LLM 실패·100자 초과 시 규칙 기반 문장, 논문이 없으면 null",
        example="역분화줄기세포 분화 조건 최적화와 오가노이드 기반 약물 평가 연구가 대부분이에요.",
    )
    summary_source: Literal["llm", "rule", "none"] = Field(
        description="요약 문장의 출처. LLM 예산 소진·응답 거부·파싱 실패 시 rule(규칙 기반)로 폴백한다"
    )
    clusters: list[ResearchFlowCluster] = Field(
        description=(
            "분야 목록. 각 분야의 마지막 논문이 최근인 순(end_year 내림차순, 같으면 마지막 논문의 발행월이 "
            "최근인 쪽, 그다음 논문 수 많은 쪽). cluster_id가 이 순서의 번호다. 개수 상한 없음"
        )
    )
