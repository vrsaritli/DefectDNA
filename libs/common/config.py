from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongodb_uri: str = ""
    mongodb_db: str = "defectdna"
    service_token_secret: str = ""
    ingestion_service_url: str = "http://127.0.0.1:8001"
    max_import_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_import_rows: int = Field(default=5_000, ge=1, le=100_000)
    ado_organization_url: str = ""
    ado_project: str = ""
    ado_pat: str = ""
    session_signing_key: str = ""
    session_cookie_secure: bool = True
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=86_400)
    session_idle_minutes: int = Field(default=30, ge=1, le=1_440)
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1_440)
    rate_limit_requests_per_minute: int = Field(default=120, ge=1, le=10_000)
    env: str = "dev"
    ai_knowledge_service_url: str = "http://127.0.0.1:8002"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    groq_max_retries: int = Field(default=3, ge=0, le=10)
    groq_reasoning_effort: str = "medium"
    llm_daily_call_cap: int = Field(default=500, ge=1, le=100_000)
    # Per Groq request: estimated prompt + expected output is packed up to the target;
    # prompt + max_completion_tokens never exceeds the budget (the rest is headroom).
    llm_request_token_budget: int = Field(default=4_500, ge=2_000, le=32_000)
    llm_batch_target_tokens: int = Field(default=4_000, ge=1_500, le=32_000)
    llm_output_tokens_per_defect: int = Field(default=400, ge=100, le=4_000)
    llm_max_defects_per_batch: int = Field(default=8, ge=1, le=25)
    max_analysis_batch: int = Field(default=200, ge=1, le=1_000)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
