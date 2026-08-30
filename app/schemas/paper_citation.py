from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.paper import PaperCardTrustBadge


class PaperCitationNode(BaseModel):
    key: str = Field(..., description="그래프 내 고유 키. in_service=true면 papers.id와 동일, false면 외부 소스 고유 ID")
    in_service: bool = Field(
        ...,
        description="서비스 코퍼스 수록 여부. 모든 노드는 클릭 가능하며 이 값으로 이동할 상세만 갈라진다 — "
        "true면 paper_id로 일반 상세페이지, false면 key로 GET /papers/citation-graph/external/{key}",
    )
    paper_id: Optional[str] = Field(default=None, description="in_service=true일 때 서비스 DB의 논문 ID. false면 null")
    title: Optional[str] = Field(default=None, description="논문 제목. 없으면 null")
    title_en: Optional[str] = Field(default=None, description="영문 제목. 없으면 null")
    pubyear: Optional[int] = Field(default=None, description="발행 연도. 없으면 null")
    tier: int = Field(..., ge=0, description="center=0, 1단계 직접 인용관계=1, 2단계(expand로만 생성)=2 ...")
    side: Literal["center", "child"] = Field(..., description="중앙 논문 자신(center)인지 하위(child)인지")
    has_more: bool = Field(
        default=False,
        description="expand 시 새로 추가될 후보가 남아있는지 여부. in_service=false 노드는 항상 false "
        "(외부 논문은 확장 불가). false면 프론트에서 expand 버튼 비활성화/숨김",
    )
    cluster_id: Optional[int] = Field(
        default=None,
        description="요약 그래프(최초 로드)의 1단계 자식 노드끼리 키워드를 일정 개수 이상 공유하면 같은 정수값 부여 "
        "(같은 값끼리 시각적으로 묶어서 표시). null이면 다른 노드와 묶이지 않은 단독 노드. "
        "center, external(in_service=false) 노드, expand로 추가된 노드는 항상 null — "
        "클러스터링은 요약 그래프의 1단계 in-service 자식에만 적용됨(외부 노드는 키워드 데이터 자체가 없음)",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "key": "ART002696010",
                    "in_service": True,
                    "paper_id": "ART002696010",
                    "title": "지속가능한 산림바이오매스 정책개발을 위한 영국사례 연구",
                    "title_en": "UK Case Study for Sustainable Forest Biomass Policy Development of South Korea",
                    "pubyear": 2021,
                    "tier": 1,
                    "side": "child",
                    "has_more": False,
                    "cluster_id": 1,
                },
                {
                    "key": "REF071661475",
                    "in_service": False,
                    "paper_id": None,
                    "title": "Directive (EU) 2023/2413 of the European parliament and of the council of 18 October 2023",
                    "title_en": None,
                    "pubyear": 2023,
                    "tier": 1,
                    "side": "child",
                    "has_more": False,
                    "cluster_id": None,
                },
            ]
        }
    }


class PaperCitationEdge(BaseModel):
    source: str = Field(..., description="인용한 논문(citing)의 key")
    target: str = Field(..., description="인용된 논문(cited)의 key")

    model_config = {
        "json_schema_extra": {
            "example": {"source": "JAKO202410843382750", "target": "ART002696010"}
        }
    }


class PaperCitationCard(BaseModel):
    """인용관계 그래프 우측 논문 리스트용 카드. in_service=false면 서지정보(제목/저자/저널/연도/doi)만
    채워지고 나머지(초록/키워드/신뢰도뱃지/북마크 등)는 전부 null — 상세페이지가 없는 외부 논문이라
    그 필드들을 계산할 근거 데이터 자체가 없기 때문."""

    key: str = Field(..., description="PaperCitationNode.key와 동일")
    in_service: bool = Field(..., description="서비스 코퍼스 수록 여부. true면 paper_id로 상세페이지 이동 가능")
    paper_id: Optional[str] = Field(default=None, description="in_service=true일 때만 서비스 DB 논문 ID")
    title: Optional[str] = Field(default=None, description="논문 제목. in_service 여부와 무관하게 항상 채워짐")
    title_en: Optional[str] = Field(default=None, description="영문 제목. 없으면 null")
    authors: Optional[list[str]] = Field(default=None, description="저자 목록. in_service 여부와 무관하게 항상 채워짐")
    journal_name: Optional[str] = Field(default=None, description="학술지명. 없으면 null")
    pub_year: Optional[int] = Field(
        default=None,
        description="발행 연도. in_service 여부와 무관하게 항상 채워짐. "
        "필드명이 PaperCardResponse와 동일(pub_year)하도록 통일 — 논문 탐색 경로 확인 페이지 리스트와 동일 형식",
    )
    doi: Optional[str] = Field(default=None, description="DOI. 없으면 null")
    abstract: Optional[str] = Field(default=None, description="in_service=true일 때만. 없으면 null")
    keywords: Optional[list[str]] = Field(default=None, description="in_service=true일 때만. 없으면 null")
    paper_type: Optional[str] = Field(default=None, description="in_service=true일 때만. 없으면 null")
    kci_registered: Optional[bool] = Field(default=None, description="in_service=true일 때만. 없으면 null")
    sci_indexed: Optional[bool] = Field(default=None, description="in_service=true일 때만. 없으면 null")
    citation_count: Optional[int] = Field(default=None, description="in_service=true일 때만. 없으면 null")
    trust_badge: Optional[PaperCardTrustBadge] = Field(default=None, description="in_service=true일 때만. 없으면 null")
    is_bookmarked: Optional[bool] = Field(default=None, description="in_service=true일 때만. 없으면 null")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "key": "ART002696010",
                    "in_service": True,
                    "paper_id": "ART002696010",
                    "title": "지속가능한 산림바이오매스 정책개발을 위한 영국사례 연구",
                    "title_en": "UK Case Study for Sustainable Forest Biomass Policy Development of South Korea",
                    "authors": ["이승록", "한규성"],
                    "journal_name": "신재생에너지",
                    "pub_year": 2021,
                    "doi": "https://doi.org/10.7849/ksnre.2021.2029",
                    "abstract": "This study investigated the reference case in the UK where legality and sustainability were systematically established...",
                    "keywords": ["산림바이오매스", "목재펠릿", "지속가능성"],
                    "paper_type": "학술 저널",
                    "kci_registered": True,
                    "sci_indexed": False,
                    "citation_count": 6,
                    "trust_badge": {"kci": True, "sci": False, "citation_count": 6, "degree_type": None},
                    "is_bookmarked": False,
                },
                {
                    "key": "REF071661475",
                    "in_service": False,
                    "paper_id": None,
                    "title": "Directive (EU) 2023/2413 of the European parliament and of the council of 18 October 2023",
                    "title_en": None,
                    "authors": ["European Parliament"],
                    "journal_name": None,
                    "pub_year": 2023,
                    "doi": None,
                    "abstract": None,
                    "keywords": None,
                    "paper_type": None,
                    "kci_registered": None,
                    "sci_indexed": None,
                    "citation_count": None,
                    "trust_badge": None,
                    "is_bookmarked": None,
                },
            ]
        }
    }


class PaperCitationGraphResponse(BaseModel):
    direction: Literal["reference", "citing"] = Field(
        ..., description="'reference'=참고문헌(이 논문이 인용한 것, backward) | 'citing'=피인용(이 논문을 인용한 것, forward)"
    )
    center: PaperCitationNode = Field(..., description="중앙(요청 대상) 논문 노드. 항상 in_service=true")
    nodes: list[PaperCitationNode] = Field(default_factory=list, description="center를 포함한 전체 노드 목록")
    edges: list[PaperCitationEdge] = Field(
        default_factory=list,
        description="center와 각 자식 노드를 잇는 방사형 엣지만 포함된다. "
        "direction=reference면 source=center/target=child, direction=citing이면 반대(source=child/target=center) — "
        "즉 reference에서는 모든 source가, citing에서는 모든 target이 center 하나로 통일된다. "
        "자식 노드끼리 잇는 엣지는 내려가지 않으므로, 프론트는 엣지의 center가 아닌 쪽 key만 보고 배치하면 된다",
    )
    has_more: bool = Field(..., description="center 기준으로 이번 응답에 다 담지 못한 후보가 더 있는지 여부 (화살표 노출용)")
    papers: list[PaperCitationCard] = Field(
        default_factory=list,
        description="현재 그래프에 표시 중인 모든 노드(center 제외)에 대응하는 카드 목록. "
        "노드 개수와 항상 1:1 대응 (in_service=false 노드도 간소화된 카드로 포함)",
    )


class PaperCitationExternalDetail(BaseModel):
    """코퍼스 밖(in_service=false) 노드를 클릭했을 때의 상세. papers 테이블에 적재된 논문이
    아니므로 PaperDetailResponse와 필드가 다르다 — 신뢰도 계산, 북마크, 유사논문, 원문 링크가
    없고 초록/키워드는 외부 조회가 성공했을 때만 채워진다.

    `enriched=false`면 저장된 서지정보(제목/저자/저널/연도/DOI)만 있는 상태다. 프론트는 이
    경우를 정상 응답으로 처리해야 하며, 초록·키워드 영역은 비워두거나 안내 문구로 대체한다."""

    key: str = Field(..., description="PaperCitationNode.key와 동일 (ART…/REF…/W… 등 외부 ID)")
    in_service: bool = Field(default=False, description="항상 false. 이 엔드포인트는 코퍼스 밖 논문 전용")
    title: Optional[str] = Field(default=None, description="논문 제목. 저장된 서지정보가 있으면 거의 항상 채워짐")
    title_en: Optional[str] = Field(default=None, description="영문 제목. 외부 조회 성공 시에만")
    authors: Optional[list[str]] = Field(default=None, description="저자 목록")
    journal_name: Optional[str] = Field(default=None, description="학술지명")
    pub_year: Optional[int] = Field(default=None, description="발행 연도")
    doi: Optional[str] = Field(default=None, description="DOI (도메인 접두어 없는 형태)")
    abstract: Optional[str] = Field(
        default=None,
        description="초록. 외부 조회가 성공하고 그쪽에 초록이 있을 때만 채워진다. "
        "ART…는 KCI에서 약 97%, W…/DOI는 OpenAlex에서 약 60% 제공되며, DOI 없는 REF…는 항상 null",
    )
    abstract_lang: Optional[Literal["ko", "en"]] = Field(
        default=None, description="초록 언어. 한글이 일정량 이상이면 'ko', 아니면 'en'. 초록이 없으면 null"
    )
    keywords: Optional[list[str]] = Field(default=None, description="키워드. KCI 경로에서만 대체로 채워짐")
    citation_count: Optional[int] = Field(default=None, description="피인용 수 (KCI 또는 OpenAlex 기준)")
    kci_registered: Optional[bool] = Field(default=None, description="KCI 등재 여부. 확인 불가면 null")
    issn: Optional[str] = Field(default=None, description="학술지 ISSN. OpenAlex 경로 약 96%, KCI 경로는 대체로 제공")
    publisher: Optional[str] = Field(default=None, description="발행처. OpenAlex 경로 약 95%, KCI 경로는 학회명")
    is_open_access: Optional[bool] = Field(
        default=None, description="오픈액세스 여부. OpenAlex 경로에서만 판정되며(약 53%가 true), 그 외에는 null"
    )
    external_url: Optional[str] = Field(
        default=None,
        description="논문으로 이동하는 링크. KCI 경로는 KCI 논문 페이지, OpenAlex 경로는 "
        "OA 원문 > 출판사 랜딩 > DOI 순으로 고른다. 저장된 DOI만 있고 외부 조회에 실패한 경우 "
        "doi.org 링크로 대체된다. 셋 다 없으면 null",
    )
    pdf_url: Optional[str] = Field(
        default=None, description="원문 PDF 직링크. OpenAlex 경로에서 약 38%만 제공되고 KCI 경로는 항상 null"
    )
    enriched: bool = Field(
        ..., description="외부 상세 조회 성공 여부. false면 저장된 서지정보만 있는 응답"
    )
    enrich_source: Optional[Literal["kci", "openalex"]] = Field(
        default=None, description="상세를 가져온 출처. enriched=false면 null"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "key": "ART001560354",
                    "in_service": False,
                    "title": "기후변화에 대응한 농업생명공학의 기회와 도전",
                    "title_en": "Agricultural biotechnology: Opportunities and challenges associated with climate change",
                    "authors": ["장안철", "최지영", "이신우"],
                    "journal_name": "Journal of Plant Biotechnology",
                    "pub_year": 2011,
                    "doi": None,
                    "abstract": "Considering that the world population is expected to total 9 billion by 2050...",
                    "abstract_lang": "en",
                    "keywords": ["agriculture", "crop", "biotechnology"],
                    "citation_count": 4,
                    "kci_registered": True,
                    "external_url": "https://www.kci.go.kr/kciportal/ci/sereArticleSearch/ciSereArtiView.kci?sereArticleSearchBean.artiId=ART001560354",
                    "enriched": True,
                    "enrich_source": "kci",
                },
                {
                    "key": "REF025243630",
                    "in_service": False,
                    "title": "Stress-induced rearrangement of Fusarium retrotransposon sequences",
                    "title_en": None,
                    "authors": ["Anaya N"],
                    "journal_name": "Mol Genl Genet",
                    "pub_year": 1996,
                    "doi": None,
                    "abstract": None,
                    "abstract_lang": None,
                    "keywords": None,
                    "citation_count": None,
                    "kci_registered": None,
                    "external_url": None,
                    "enriched": False,
                    "enrich_source": None,
                },
            ]
        }
    }


class PaperCitationExpandRequest(BaseModel):
    direction: Literal["reference", "citing"] = Field(..., description="확장 시 적용할 방향 (현재 화면의 토글 상태와 동일해야 함)")
    existing_node_keys: list[str] = Field(..., description="현재 화면에 표시 중인 모든 노드의 key (전역 dedup 및 100개 캡 계산용)")
    current_tier: int = Field(..., ge=0, description="확장 대상 노드의 현재 tier. 신규 노드는 이 값+1로 배정됨")

    model_config = {
        "json_schema_extra": {
            "example": {
                "direction": "reference",
                "existing_node_keys": ["JAKO202410843382750", "ART002696010"],
                "current_tier": 1,
            }
        }
    }


class PaperCitationExpandResponse(BaseModel):
    parent_key: str = Field(..., description="확장 대상이 된 노드의 key (요청의 node_key와 동일)")
    direction: Literal["reference", "citing"] = Field(..., description="요청에 사용된 방향, 그대로 반환")
    new_nodes: list[PaperCitationNode] = Field(default_factory=list, description="이번 확장으로 새로 추가된 노드 목록")
    new_edges: list[PaperCitationEdge] = Field(default_factory=list, description="new_nodes에 대응하는 신규 엣지 목록")
    parent_has_more: bool = Field(default=False, description="이번 expand로 다 못 가져온, parent_key의 남은 후보가 더 있는지 여부")
    capped: bool = Field(
        default=False,
        description="전체 노드 100개 제한에 걸려 이번 확장이 (부분적으로 또는 전혀) 반영되지 않았는지 여부. "
        "true면 프론트에서 '노드는 100개까지 표시할 수 있어요' 안내 노출",
    )
    papers: list[PaperCitationCard] = Field(
        default_factory=list, description="new_nodes에 대응하는 카드 목록 (우측 리스트 증분 갱신용)"
    )
