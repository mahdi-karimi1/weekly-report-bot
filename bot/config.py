"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Credentials must come from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")

    avalai_api_key: str = Field(default="", alias="AVALAI_API_KEY")
    # Native GenAI / v1beta base URL — must NOT include /v1
    avalai_base_url: str = Field(
        default="https://api.avalai.ir",
        alias="AVALAI_BASE_URL",
    )
    # OpenAI-compatible base URL — must include /v1
    avalai_openai_base_url: str = Field(
        default="https://api.avalai.ir/v1",
        alias="AVALAI_OPENAI_BASE_URL",
    )

    model: str = Field(default="gemini-3.1-pro-preview", alias="MODEL")

    # Text / structured extraction backend:
    # - auto: try OpenAI-compatible first, fall back to native GenAI on notice/failure
    # - openai: OpenAI-compatible only
    # - native: native GenAI only
    text_generation_backend: Literal["auto", "openai", "native"] = Field(
        default="auto",
        alias="TEXT_GENERATION_BACKEND",
    )

    database_url: str = Field(default="sqlite:///bot.db", alias="DATABASE_URL")

    enable_fallback_transcription: bool = Field(
        default=False,
        alias="ENABLE_FALLBACK_TRANSCRIPTION",
    )
    fallback_transcription_base_url: str = Field(
        default="",
        alias="FALLBACK_TRANSCRIPTION_BASE_URL",
    )
    fallback_transcription_api_key: str = Field(
        default="",
        alias="FALLBACK_TRANSCRIPTION_API_KEY",
    )
    fallback_transcription_model: str = Field(
        default="",
        alias="FALLBACK_TRANSCRIPTION_MODEL",
    )

    excel_template_path: Path = Field(
        default=Path("templates/weekly_report.xlsx"),
        alias="EXCEL_TEMPLATE_PATH",
    )
    excel_mapping_path: Path = Field(
        default=Path("templates/weekly_report_mapping.json"),
        alias="EXCEL_MAPPING_PATH",
    )
    output_dir: Path = Field(default=Path("outputs"), alias="OUTPUT_DIR")
    temp_audio_dir: Path = Field(default=Path("temp_audio"), alias="TEMP_AUDIO_DIR")

    max_audio_bytes: int = Field(default=20 * 1024 * 1024, alias="MAX_AUDIO_BYTES")
    api_timeout_seconds: float = Field(default=120.0, alias="API_TIMEOUT_SECONDS")
    api_max_retries: int = Field(default=3, alias="API_MAX_RETRIES")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        alias="LOG_LEVEL",
    )
    log_transcriptions: bool = Field(default=False, alias="LOG_TRANSCRIPTIONS")

    @field_validator("avalai_base_url")
    @classmethod
    def _strip_trailing_slash_native(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("avalai_openai_base_url")
    @classmethod
    def _normalize_openai_base(cls, value: str) -> str:
        cleaned = value.rstrip("/")
        if cleaned.endswith("/v1"):
            return cleaned
        # Keep /v1 separate so callers do not accidentally double-append paths.
        return f"{cleaned}/v1"

    def ensure_runtime_dirs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_audio_dir.mkdir(parents=True, exist_ok=True)
        self.excel_template_path.parent.mkdir(parents=True, exist_ok=True)

    def require_telegram_token(self) -> str:
        if not self.telegram_bot_token.strip():
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and set it."
            )
        return self.telegram_bot_token

    def require_avalai_key(self) -> str:
        if not self.avalai_api_key.strip():
            raise ValueError(
                "AVALAI_API_KEY is missing. Copy .env.example to .env and set it."
            )
        return self.avalai_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
