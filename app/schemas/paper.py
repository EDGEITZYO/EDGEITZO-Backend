from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.search import CredibilityInfo
from app.schemas.trust_badge import TrustBadge


class PaperCardTrustBadge(BaseModel):
    """논문 카드(목록)에서 사용하는 신뢰도 뱃지. 상세 페이지용 TrustBadge와 별개."""
    kci: Optional[bool] = Field(None, description="KCI 등재 여부. papers.db_code='JAKO'이면 true", example=True)
    sci: Optional[bool] = Field(None, description="SCI 계열 등재 여부. journals.sci_indexed 기준. 현재 미수집으로 대부분 false", example=False)
    citation_count: Optional[int] = Field(None, description="인용 수. papers.citation_count 기준. null이면 미집계", example=15)
    degree_type: Optional[str] = Field(None, description="학위 구분. '박사 학위 논문' | '석사 학위 논문' | null. 학위논문 유형 배지로 활용", example=None)


class PaperCardResponse(BaseModel):
    """키워드 검색 / 키워드맵 노드 논문 목록에서 공통으로 사용하는 논문 카드 스키마."""
    paper_id: str = Field(
        description="논문 고유 ID. papers 테이블 id (CN 형식)",
        example="JAKO202509339655899",
    )
    title: str = Field(
        description="논문 제목 (한국어)",
        example="Streptococcus mutans의 성장 및 구강 바이오필름 형성에 대한 wasabi 유래 성분 연구",
    )
    authors: List[str] = Field(
        description="저자 목록. ChromaDB Author 필드 기준",
        example=["조하랑", "김기림"],
    )
    pub_year: Optional[int] = Field(
        None,
        description="발행 연도. ChromaDB Pubyear 기준. 없으면 null",
        example=2025,
    )
    journal_name: Optional[str] = Field(
        None,
        description="학술지명. ChromaDB JournalName 기준. 학위논문/미등록 시 null",
        example="한국임상치위생학회지 = Korean journal of clinical dental hygiene",
    )
    paper_type: Optional[str] = Field(
        None,
        description="논문 유형. '박사학위 논문' | '석사학위 논문' | '학술 저널' | null",
        example="학술 저널",
    )
    abstract: Optional[str] = Field(
        None,
        description="초록 원문. ChromaDB Abstract 기준. 없으면 null",
        example="This study evaluated the antibacterial and antibiofilm activities of wasabi-derived 6-(methylsulfinyl)hexyl isothiocyanate against Streptococcus mutans.",
    )
    keywords: List[str] = Field(
        description="논문 키워드 목록. ChromaDB Keyword 기준",
        example=["6-(메틸설피닐)헥실 이소티오시아네이트", "항균", "항바이오필름 스트렙토코쿠스 뮤탄스"],
    )
    doi: Optional[str] = Field(
        None,
        description="DOI URL. ChromaDB DOI 기준. 없으면 null",
        example="https://doi.org/10.12972/kjcdh.2025.13.4.2",
    )
    kci_registered: bool = Field(
        description="KCI 등재 여부. papers.db_code = 'JAKO'이면 true. PostgreSQL IN 쿼리로 확인",
        example=True,
    )
    sci_indexed: bool = Field(
        description="SCI 계열 등재 여부. journals.sci_indexed 기준 (papers.journal_id JOIN). 현재 SCI 논문 미수집으로 대부분 false",
        example=False,
    )
    citation_count: Optional[int] = Field(
        None,
        description="인용 수. papers.citation_count 기준. PostgreSQL IN 쿼리로 조회. papers 테이블에 없는 논문은 null",
        example=15,
    )
    relevance_score: float = Field(
        description="ChromaDB RRF(Reciprocal Rank Fusion) 유사도 점수. 키워드 직접 조회(get_by_ids)시 0.0 고정, 시맨틱 검색 시 0~1 범위",
        example=0.8732,
    )
    trust_badge: Optional[PaperCardTrustBadge] = Field(
        None,
        description="신뢰도 뱃지. kci/sci/citation_count/degree_type 포함. papers 테이블에 없는 논문은 null",
    )
    is_bookmarked: bool = Field(
        False,
        description="요청자가 이 논문을 북마크했는지 여부. user_id 미제공(비로그인) 시 항상 false",
    )


class PaperListResponse(BaseModel):
    """키워드 검색 / 키워드맵 노드 논문 목록 공통 응답 래퍼."""
    keyword: str = Field(
        description="검색에 사용한 키워드",
        example="딥러닝",
    )
    papers: List[PaperCardResponse] = Field(
        description="논문 카드 목록. 필터/정렬 적용 후 전체 결과 (페이지네이션 없음)",
    )
    total: int = Field(
        description="필터 적용 후 전체 결과 수 (papers의 길이와 동일)",
        example=142,
    )
    search_id: Optional[str] = Field(
        None,
        description="탐색 세션 ID. 논문 열람 시 POST /home/recent-reads의 search_id로 전달하면 last_viewed_paper_title 갱신됨",
        example="3f82a1c0-1234-4abc-8def-000000000000",
    )


class PaperDetailResponse(BaseModel):
    paper_id: str = Field(description="논문 고유 ID", example="JAKO202312345678")
    title: str = Field(description="논문 제목")
    title_en: Optional[str] = Field(None, description="영문 제목. 없으면 null")
    authors: Optional[list[str]] = Field(None, description="저자 목록")
    abstract: Optional[str] = Field(None, description="초록")
    abstract_en: Optional[str] = Field(None, description="영문 초록. 없으면 null")
    keywords_ko: Optional[list[str]] = Field(None, description="한국어 키워드")
    keywords_en: Optional[list[str]] = Field(None, description="영문 키워드")
    published_at: Optional[str] = Field(None, description="발행일 (ISO8601). pubdate 우선, 없으면 '{year}-01-01'")
    paper_type: Optional[str] = Field(None, description="논문 유형. '박사학위 논문' | '석사학위 논문' | '학술 저널' | null")
    journal_name: Optional[str] = Field(None, description="학술지명. 없으면 null")
    doi: Optional[str] = Field(None, description="DOI. 없으면 null")
    citation_count: Optional[int] = Field(None, description="인용 수. 없으면 null")
    degree: Optional[str] = Field(None, description="학위 구분 (학위논문만). 없으면 null")
    affiliation: Optional[str] = Field(None, description="소속 기관 (학위논문만). 없으면 null")
    fulltext_flag: Optional[bool] = Field(None, description="원문 제공 여부. 없으면 null")
    credibility: CredibilityInfo = Field(description="신뢰도 정보")
    trust_badge: TrustBadge = Field(description="신뢰도 뱃지")


class SimilarPaperResponse(BaseModel):
    title: Optional[str] = Field(None, description="유사 논문 제목. 없으면 null")
    author: Optional[str] = Field(None, description="저자. 없으면 null")
    pubyear: Optional[int] = Field(None, description="발행 연도. 없으면 null")
    material_type: Optional[str] = Field(None, description="자료 유형. 없으면 null")
    in_service: bool = Field(description="서비스 DB(papers 테이블) 수록 여부. true 시 paper_id로 이동 가능")
    paper_id: Optional[str] = Field(None, description="in_service=true일 때 서비스 DB의 논문 ID. false 시 null")
    journal_name: Optional[str] = Field(None, description="학술지명. in_service=true이고 저널 매칭 시. 없으면 null")
    keywords: Optional[List[str]] = Field(None, description="한국어 키워드. in_service=true일 때. 없으면 null")
    doi: Optional[str] = Field(None, description="DOI. in_service=true일 때. 없으면 null")
    trust_badge: Optional[TrustBadge] = Field(None, description="신뢰도 뱃지. in_service=true일 때. 없으면 null")


class ReferenceResponse(BaseModel):
    doi: Optional[str] = Field(None, description="참고문헌 DOI. 없으면 null", example="10.1234/example")
    title: Optional[str] = Field(None, description="참고문헌 제목. 없으면 null")
    authors: Optional[list[str]] = Field(None, description="저자 목록. 없으면 null", example=["홍길동", "김철수"])
    year: Optional[int] = Field(None, description="발행 연도. 없으면 null", example=2023)
    journal: Optional[str] = Field(None, description="학술지명. 없으면 null")
    in_service: bool = Field(description="서비스 DB(papers 테이블) 수록 여부. true 시 paper_id 사용 가능")
    paper_id: Optional[str] = Field(None, description="in_service=true일 때 서비스 DB의 논문 ID. false 시 null")
    unstructured: Optional[str] = Field(None, description="CrossRef fallback 시 원문 인용 문자열. ScienceON 논문은 null")
