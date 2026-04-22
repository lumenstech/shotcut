from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")

    database_url: str = Field(
        "postgresql+asyncpg://shotcut:shotcut@localhost:5432/shotcut",
        alias="DATABASE_URL",
    )

    planner_model: str = Field("claude-opus-4-7", alias="PLANNER_MODEL")
    executor_model: str = Field("claude-opus-4-7", alias="EXECUTOR_MODEL")
    verifier_model: str = Field("claude-opus-4-7", alias="VERIFIER_MODEL")
    researcher_model: str = Field("claude-haiku-4-5", alias="RESEARCHER_MODEL")

    storage_dir: Path = Field(Path("./storage"), alias="STORAGE_DIR")
    edgar_identity: str = Field("shotcut-dev dev@example.com", alias="EDGAR_IDENTITY")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    max_upload_mb: int = Field(50, alias="MAX_UPLOAD_MB")


# Pydantic-settings reads all fields from env/.env; mypy's strict mode
# doesn't model that, so it insists we pass each alias as a kwarg.
settings = Settings()  # type: ignore[call-arg]
settings.storage_dir.mkdir(parents=True, exist_ok=True)
