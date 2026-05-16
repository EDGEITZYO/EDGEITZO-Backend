from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "PaperGraph Agent API"
    app_env: str = "local"
    app_port: int = 8000

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()