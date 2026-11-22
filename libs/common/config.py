from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    mongodb_uri: str = ""
    mongodb_db: str = "defectdna"
    service_token_secret: str = ""
    session_signing_key: str = ""
    session_cookie_secure: bool = True
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=86_400)
    session_idle_minutes: int = Field(default=30, ge=1, le=1_440)
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1_440)
    rate_limit_requests_per_minute: int = Field(default=120, ge=1, le=10_000)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
