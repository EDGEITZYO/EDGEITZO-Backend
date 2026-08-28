from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SearchPapersRequest(BaseModel):
    query: str = Field(..., min_length=1, description="사용자 검색어")
    pub_year_start: Optional[int] = Field(
        None, description="이 연도 이상만 포함. 미설정 시 전체 연도"
    )
    paper_type: Optional[Literal["JAKO", "DIKO", "JAFO", "CFKO"]] = Field(
        None,
        description="논문 유형(DBCode 원본값)으로 좁히기. 'JAKO'=국내 학술지 | 'DIKO'=학위논문 | 'JAFO'=해외 학술지 | 'CFKO'=학술대회. 미설정 시 전체 유형",
    )
    kci_only: bool = Field(False, description="true면 KCI 등재 논문만 포함")
    sci_only: bool = Field(False, description="true면 SCI 계열(SCIE/SSCI/AHCI) 등재 논문만 포함")
    keywords: list[str] = Field(
        default_factory=list,
        description="추출/선택된 키워드 목록. query와 함께 검색어에 반영됨",
    )
    size: Optional[int] = Field(
        default=None,
        ge=1,
        description="반환할 최대 건수. 생략하면 필터링·정렬까지 마친 전체 결과를 반환 (페이지 개념 없음 — 프론트에서 무한스크롤로 렌더링)",
    )
    sort_order: Literal["relevance", "year_asc", "year_desc", "citation_desc"] = Field(
        default="relevance",
        description="정렬 기준. 'relevance': 관련도순(기본값) / 'year_desc': 최신순 / 'year_asc': 오래된순 / 'citation_desc': 인용수 높은순",
    )


class PaperAuthor(BaseModel):
    name: str = Field(description="저자명")
    affiliation: Optional[str] = Field(None, description="소속 기관. 정보 없으면 null")


class CredibilityInfo(BaseModel):
    badge: Literal["high", "medium", "low", "unknown"] = Field(
        "unknown", description="종합 신뢰도 등급. 인용수·SJR 사분위·SCI/KCI 등재 여부를 종합 판정"
    )
    citation_count: Optional[int] = Field(None, description="인용 횟수. 정보 없으면 null")
    citation_badge: Optional[str] = Field(None, description="인용수 배지 문구. 예: 'Citations 42'. citation_count 없으면 null")
    impact_factor: Optional[float] = Field(None, description="저널 Impact Factor. 정보 없으면 null")
    impact_factor_badge: Optional[str] = Field(None, description="IF 배지 문구. 예: 'IF 3.5'. impact_factor 없으면 null")
    kci_registered: Optional[bool] = Field(None, description="KCI 등재 여부. 알 수 없으면 null")
    kci_badge: Optional[str] = Field(None, description="KCI 등재 배지 문구. 'KCI O' | 'KCI X' | 'KCI unknown'")
    sci_indexed: Optional[bool] = Field(None, description="SCI 계열(SCIE/SSCI/AHCI) 등재 여부. 알 수 없으면 null")
    sci_badge: Optional[str] = Field(None, description="SCI 계열 등재 배지 문구. 'SCI O' | 'SCI X' | 'SCI unknown'")
    sjr_quartile: Optional[str] = Field(None, description="SJR(Scimago Journal Rank) 사분위. 'Q1'~'Q4'. 정보 없으면 null")
    sjr_score: Optional[float] = Field(None, description="SJR 점수. 정보 없으면 null")
    h_index: Optional[int] = Field(None, description="저널 h-index. 정보 없으면 null")
    summary: Optional[str] = Field(None, description="신뢰도 판정 근거 요약(영문, 내부 로직 설명용). 예: 'citation_count=42 / SJR Q1'")


class PaperSearchItem(BaseModel):
    paper_id: str = Field(description="논문 고유 ID (ScienceON CN)")
    title: str = Field(description="논문 제목")
    authors: list[PaperAuthor] = Field(default_factory=list, description="저자 목록")
    year: Optional[int] = Field(None, description="발행 연도. 정보 없으면 null")
    abstract: Optional[str] = Field(None, description="초록 원문. 정보 없으면 null")
    keywords: list[str] = Field(default_factory=list, description="논문 원본 키워드 목록")
    journal_name: Optional[str] = Field(None, description="학술지명. 학위논문 등은 null")
    issn: Optional[str] = Field(None, description="ISSN. 정보 없으면 null")
    doi: Optional[str] = Field(None, description="DOI. 정보 없으면 null")
    db_code: Optional[str] = Field(
        None, description="ScienceON DB 코드. 'JAKO'=국내 학술지 | 'DIKO'=학위논문 | 'JAFO'=해외 학술지 | 'CFKO'=학술대회"
    )
    paper_type: Optional[str] = Field(
        None, description="논문 유형 배지. '학술 저널' | '박사학위 논문' | '석사학위 논문' | null. UI 배지 표시용 — db_code 대신 이 필드를 사용할 것"
    )
    source: str = Field(description="검색 소스. 현재 항상 'local_chroma'")
    is_bookmarked: bool = Field(False, description="요청자가 이 논문을 북마크했는지 여부. 비로그인 요청이면 항상 false")
    credibility: CredibilityInfo = Field(description="신뢰도 배지/지표 정보")
    score: float = Field(description="정렬에 쓰이는 관련도 점수. 시맨틱+키워드(BM25) 검색 융합(RRF) 점수 기준 — 값 자체의 절대적 의미보다 상대적 순위 비교용")
    similarity_score: float = Field(
        description="검색어와 논문(제목+초록 임베딩) 간 코사인 유사도. 0~1. 이 논문이 추천된 핵심 근거"
    )
    matched_snippet: Optional[str] = Field(
        None,
        description="초록 문장 중 검색어와 가장 의미적으로 유사한 문장 1개. 추천 근거로 UI에 하이라이트 노출용. 초록이 없으면 null",
    )


class SearchPapersResponse(BaseModel):
    search_id: str = Field(description="검색 요청 식별자 (UUID). 요청마다 새로 발급됨")
    items: list[PaperSearchItem] = Field(description="검색 결과 논문 목록. score(관련도) 내림차순 정렬")


class SearchParamsDoc(BaseModel):
    """SearchParams TypedDict의 Swagger 노출용 Pydantic 래퍼."""
    keywords: List[str] = Field(description="검색 키워드 목록", example=["딥러닝", "CNN"])
    scope: str = Field(description="논문 범위. 'KCI' | 'SCI' | 'ALL' | 'ANY'", example="KCI")
    pub_year_start: Optional[int] = Field(None, description="발행 연도 시작. 예: 2021 (5Y 선택 시)", example=2021)
    research_purpose: str = Field(description="연구 목적. '연구주제탐색' | '논문작성참고' | '랩미팅발표' | '최신트렌드'", example="연구주제탐색")
    trust_level: Optional[str] = Field(None, description="신뢰도 필터. 현재 항상 null", example=None)
    advanced_filters: Dict[str, Any] = Field(default_factory=dict, description="고급 필터. 예: {'extra': '한국 저자만'}", example={"extra": "한국 저자만"})


# ── 논문 선정 사유 (명세 02-11) ────────────────────────────────────────


class SelectionReasonRequest(BaseModel):
    """지금 화면에 보이는 논문들의 선정 사유를 요청한다.

    '상위 N건'이 아니라 '뷰포트에 들어온 것' 기준으로 보내야 한다 — 정렬을 바꾸거나
    필터를 걸면 상위 목록이 통째로 달라지므로, 상위 N건으로 미리 만들어두면 화면에는
    사유가 있는 카드와 없는 카드가 섞이게 된다.
    """

    keywords: list[str] = Field(
        ...,
        min_length=1,
        description="이번 검색의 키워드 목록. 캐시 키가 되므로 검색에 쓴 값과 같아야 함 "
                    "(자연어 검색은 `filters.keywords`, 단순 검색은 요청 시 보낸 keywords 또는 query)",
    )
    paper_ids: list[str] = Field(
        ...,
        min_length=1,
        # 서버의 동시 생성 수(settings.selection_reason_concurrency)와 같은 값으로 맞춘다.
        # 예전엔 30이었는데, 그러면 30건 요청이 10건씩 3라운드로 쪼개져 소요가 그대로 3배가
        # 됐다(4초 → 12초). 게다가 그 30건 중 실제로 화면에 보이는 건 앞부분뿐이라,
        # 안 보이는 카드를 만드느라 보이는 카드가 늦게 채워졌다.
        # 더 필요하면 두 번 부르는 편이 낫다 — 먼저 온 10건이 먼저 화면에 채워진다.
        max_length=10,
        description="사유가 필요한 논문 ID. 한 번에 최대 10건 — 화면에 보이는 만큼만 보낼 것. "
                    "더 필요하면 여러 번 나눠 호출할 것(한 번에 몰아 보내면 서버가 어차피 "
                    "10건씩 쪼개 처리해 그만큼 느려진다)",
    )


class SelectionReasonItem(BaseModel):
    paper_id: str = Field(description="논문 고유 ID")
    reason: Optional[str] = Field(
        None,
        description="선정 사유 본문 (공백 포함 150~200자, 3문장). 드물게 220자까지 나올 수 있다 "
                    "— 상한을 몇 자 넘었다고 문장을 잘라내면 3번째 문장이 통째로 사라지므로 "
                    "그대로 내보낸다. 레이아웃은 220자 기준으로 잡을 것. "
                    "초록이 없거나 생성에 실패하면 null — 이때 프런트는 사유 영역을 비우거나 숨긴다",
    )
    highlight_start: Optional[int] = Field(
        None,
        description="강조할 구절의 `reason` 내 시작 위치(0-based, 파이썬 문자열 인덱스 = 코드포인트 단위). "
                    "강조 구절을 특정하지 못하면 null이며, 이때도 `reason`은 정상",
    )
    highlight_end: Optional[int] = Field(
        None, description="강조할 구절의 끝 위치(exclusive). `reason[highlight_start:highlight_end]`가 강조 대상"
    )
    cached: bool = Field(
        False, description="true면 기존에 만들어둔 것을 재사용 (LLM 호출 없음)"
    )


class SelectionReasonResponse(BaseModel):
    items: list[SelectionReasonItem] = Field(
        description="요청한 paper_ids 순서와 무관. 사유를 만들지 못한 논문도 reason=null로 포함됨"
    )
