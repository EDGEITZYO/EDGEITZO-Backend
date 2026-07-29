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
    llm_default_model: str = "claude-haiku-4-5"
    llm_budget_total_usd: float = 40.0
    llm_timeout_seconds: int = 120
    graph_timeout_seconds: int = 300

    # CrossRef polite mode
    crossref_contact_email: str = "yuri12120771@gmail.com"

    # SSE 스트리밍
    sse_chunk_size: int = 2
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
    keyword_map_parent_max: int = 4
    keyword_map_child_l1_max: int = 6
    keyword_map_expand_max: int = 3
    keyword_map_hub_cross_link_threshold: int = 3
    keyword_map_cache_ttl_seconds: int = 300
    keyword_map_definition_llm_max_abstracts: int = 5

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
