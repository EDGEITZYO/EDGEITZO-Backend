from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "PaperGraph Agent API"
    app_env: str = "local"
    app_port: int = 8000

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = Field(
        default="neo4j",
        validation_alias=AliasChoices("NEO4J_USER", "NEO4J_USERNAME"),
    )
    neo4j_password: str = "password1234"

    chroma_host: str = "localhost"
    chroma_port: int = 8001

    scienceon_base_url: str = "https://apigateway.kisti.re.kr/openapicall.do"
    scienceon_client_id: str = ""
    scienceon_token: str = ""
    scienceon_version: str = "1.0"

    semantic_scholar_base_url: str = "https://api.semanticscholar.org/graph/v1"
    semantic_scholar_api_key: str = ""

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/edgeitzo"

    kakao_client_id: str = ""
    kakao_client_secret: str = ""
    kakao_redirect_uri: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    jwt_secret_key: str = ""
    jwt_access_expire_minutes: int = 30
    jwt_refresh_expire_days: int = 7

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    frontend_url: str = "http://localhost:3000"

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [
            self.frontend_url,
            "http://localhost:5173",
            "http://localhost:5173/",
            "https://edgeitzo-frontend-git-dev-minju3212s-projects.vercel.app",
        ]

    # Celery — Redis DB 1(broker), DB 2(result) 사용. DB 0은 auth 캐시용으로 예약.
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # 토큰 블랙리스트 전용 Redis DB
    redis_blacklist_db: int = 3

    # KCI
    kci_api_key: str = ""
    kci_base_url: str = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"

    # LLM
    anthropic_api_key: str = ""
    # 모델은 이름이 아니라 용도(등급)로 고른다 — "haiku"/"sonnet"으로 변수를 지으면
    # 모델을 교체하는 순간 변수명이 거짓말을 한다. 새 모델로 갈아탈 때 여기 두 줄만 고치면
    # 해당 등급을 쓰는 기능이 전부 따라 옮겨간다.
    #
    # fast    : 결과가 코드로 다시 가공되는 내부 처리 — 분류, 추출, 키워드
    # quality : 사용자에게 문장 그대로 노출되는 글 — 선정 사유, 키워드 정의, AI 요약
    #           비용 때문에 fast로 내리지 말 것. 작은 모델은 "검색어와 접점이 약할 때
    #           억지 연결을 하지 않는" 판단에 자주 실패하고, 그 실패가 '없는 연결을
    #           지어내는' 형태로 나타나 이 기능들의 목적을 정면으로 훼손한다.
    #
    # 주의: 날짜 꼬리가 붙은 ID(claude-haiku-4-5-20251001)를 쓰지 말 것. 동작은 하지만
    # llm/client.py의 단가표·파라미터 예외 목록이 접두 매칭으로 흡수해야 하는 부담이 된다.
    llm_model_fast: str = "claude-haiku-4-5"
    llm_model_quality: str = "claude-sonnet-5"
    llm_budget_total_usd: float = 40.0
    llm_timeout_seconds: int = 120
    graph_timeout_seconds: int = 300

    # CrossRef polite mode
    crossref_contact_email: str = "yuri12120771@gmail.com"

    # SSE 스트리밍
    sse_chunk_size: int = 2
    # SSE로 먼저 흘려보낼 선정 사유 건수. 화면 첫 화면에 보이는 만큼만 만들고,
    # 스크롤·정렬·필터로 새로 보이는 것은 프런트가 POST /search/selection-reasons로 받는다.
    search_selection_reason_initial_count: int = 10
    # 선정 사유를 몇 번 뽑아 그중 가장 좋은 것을 고를지 (Best-of-N). 1이면 단발 호출.
    # N개를 병렬로 던지므로 지연은 1회 호출과 거의 같고, 비용만 N배에 가까워진다.
    # 실측(23건): 순차 재시도 적중 26%·14초·$0.0111 vs Best-of-2 적중 43%·8초·$0.0123.
    selection_reason_best_of: int = 2
    # 선정 사유 길이 정책 — 명세가 무엇을 요구하느냐에 맞춰 고른다.
    #   "center"    : 목표 구간 한가운데(175자)를 겨냥. 명세가 "150~200자"처럼 범위일 때.
    #                 실측 20건: 준수 90%, 중앙값 176자, 실제 135~195자.
    #                 (옛 명세 170~180 기준으로는 같은 표본에서 준수 30%였다 — 11자짜리
    #                  창은 LLM이 자기 글자수를 못 세는 이상 구조적으로 못 맞춘다)
    #   "under_max" : 상한 이하 중 최장을 채택. 명세가 "200자 이내"처럼 상한만 있을 때.
    #                 상한 180이던 시절 준수 100%였으나 상한 200으로는 재측정 안 됨.
    selection_reason_length_policy: str = "center"
    # 선정 사유 동시 생성 수. 초기 노출 건수(10)와 맞춰 한 라운드에 끝나게 한다 —
    # 5로 두면 10건이 2라운드가 되어 소요가 그대로 두 배(8초 → 16초)가 된다.
    selection_reason_concurrency: int = 10
    sse_chunk_delay_seconds: float = 0.04
    sse_heartbeat_seconds: float = 15.0

    # 좁히기 칩 — 분포 편차 계산
    chip_bin_count: int = 3
    chip_evenness_threshold: float = 0.7

    # 자유입력 광범위 질문 판정 — 결과 10건 이상이면 광범위한 검색으로 판정
    search_broad_result_threshold: Optional[int] = 10

    # 관련도 하한선 — 1위 논문의 similarity_score 대비 이 비율 미만인 후보는 제외.
    # 절대 점수는 코퍼스/질의마다 분포가 달라 고정 임계값으로 못 쓰므로(실측 확인됨),
    # "1등 대비 상대적으로 얼마나 안 맞는지"를 기준으로 삼는다. 테스트하며 조정 예정.
    search_relevance_ratio: float = 0.7

    # 키워드맵 — 빈도/동시출현 기반 그래프 생성 튜닝값
    keyword_map_candidate_pool_size: int = 40
    keyword_map_max_nodes: int = 25
    keyword_map_child_l1_max: int = 6
    keyword_map_expand_max: int = 3
    keyword_map_hub_cross_link_threshold: int = 3
    keyword_map_cache_ttl_seconds: int = 300
    keyword_map_definition_llm_max_abstracts: int = 5

    # 논문 인용관계(참고문헌/피인용) 그래프 — 자체 코퍼스 내부 CITES 관계 기반
    paper_citation_max_nodes: int = 100
    paper_citation_summary_limit: int = 12
    paper_citation_expand_max: int = 6
    paper_citation_cache_ttl_seconds: int = 300
    # 요약 그래프(07-01) 클러스터링 — 1단계 in-service 자식끼리 이 개수 이상 키워드를 공유하면 같은 그룹으로 묶음
    paper_citation_cluster_min_shared_keywords: int = 2

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
