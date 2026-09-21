"""Конфигурация проекта. Единственное место, где читается окружение.

Все значения приходят из .env / переменных окружения через pydantic-settings.
Секретов в коде нет и быть не должно (см. docs/TZ.md, раздел 5).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

AppRole = Literal["collector", "bot", "web"]


class Settings(BaseSettings):
    """Настройки приложения. Имена полей совпадают с ключами .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_SESSION: str = "./data/collector.session"
    TG_SESSION_STRING: str = ""  # StringSession для деплоя, файл .session не нужен
    TG_GROUP_ID: int = 0  # только для первого запуска, дальше группы из БД
    BOT_TOKEN: str = ""
    OWNER_ID: int = 0
    TELEGRAPH_TOKEN: str = ""
    TELEGRAPH_SHORT_NAME: str = "TeleTemaBot"

    # --- LLM (OpenAI-совместимый провайдер) ---
    LLM_BASE_URL: str = "https://api.z.ai/api/paas/v4"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "glm-5.3-flash"
    LLM_REASONING_EFFORT: str = "low"

    # --- Эмбеддинги ---
    EMBED_BACKEND: Literal["local", "api"] = "local"
    EMBED_MODEL: str = "BAAI/bge-m3"
    EMBED_DIM: int = 1024

    # --- БД ---
    DATABASE_URL: str = ""

    # --- ASR ---
    WHISPER_MODEL: str = "small"
    WHISPER_DEVICE: str = "cpu"

    # --- Сеть / прочее ---
    PROXY_URL: str = ""
    TZ: str = "Asia/Kamchatka"
    DATA_DIR: Path = ROOT / "data"
    LOG_LEVEL: str = "INFO"
    APP_ROLE: AppRole = "bot"

    # --- Веб-админка ---
    WEB_SECRET_KEY: str = ""
    WEB_BASE_URL: str = ""
    WEB_HOST: str = "0.0.0.0"
    WEB_PORT: int = 8080

    @field_validator("TG_GROUP_ID", "OWNER_ID", "TG_API_ID", mode="before")
    @classmethod
    def _empty_int(cls, v: Any) -> Any:
        """Пустая строка в .env означает «не задано», а не ошибку валидации."""
        if isinstance(v, str) and not v.strip():
            return 0
        return v

    @property
    def llm_extra_body(self) -> dict[str, Any]:
        """Доп. поля запроса к LLM: reasoning отключить нельзя, но можно занизить."""
        if not self.LLM_REASONING_EFFORT:
            return {}
        return {"reasoning_effort": self.LLM_REASONING_EFFORT}

    @property
    def media_dir(self) -> Path:
        return self.DATA_DIR / "media"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


cfg: Settings = get_settings()
