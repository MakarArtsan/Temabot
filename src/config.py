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

# migrate — разовый запуск миграций тем же образом (docker/entrypoint.sh)
AppRole = Literal["all", "collector", "bot", "web", "migrate"]


class Settings(BaseSettings):
    """Настройки приложения. Имена полей совпадают с ключами .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # значения часто вставляют с телефона — лишний пробел по краям не должен ломать запуск
        str_strip_whitespace=True,
    )

    # --- Telegram ---
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_SESSION: str = "./data/collector.session"
    TG_SESSION_STRING: str = ""  # StringSession для деплоя, файл .session не нужен
    TG_GROUP_ID: int = 0  # только для первого запуска, дальше группы из БД
    # collector при первом старте сам заливает историю за столько дней; 0 — не заливать
    BACKFILL_DAYS: int = 7
    BOT_TOKEN: str = ""
    # Адрес Bot API. Пусто — официальный api.telegram.org; для локальных проверок
    # сюда ставится подставной сервер, чтобы ничего не ушло в живой Telegram
    BOT_API_URL: str = ""
    OWNER_ID: int = 0
    TELEGRAPH_TOKEN: str = ""
    TELEGRAPH_SHORT_NAME: str = "TeleTemaBot"
    BOT_USERNAME: str = ""            # без «собаки»; нужен виджету входа в админку
    # telegraph — как в исходнике: страница открывается любым, у кого есть ссылка.
    # dm — текст приходит в личку запросившему и наружу не уходит (TZ §4.6).
    COPY_MODE: str = "telegraph"

    # --- LLM (OpenAI-совместимый провайдер) ---
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-flash"
    # deepseek-flash и GLM — reasoning-модели: без ограничения на разбор одного треда
    # уходят сотни лишних токенов, а в JSON-режиме ответ может вернуться пустым,
    # потому что весь лимит съеден размышлением. Замеры — в docs/SETUP.md.
    LLM_THINKING: str = "disabled"     # disabled | auto
    LLM_REASONING_EFFORT: str = ""     # для провайдеров, где thinking не отключается

    # --- Эмбеддинги через API (у DeepSeek их нет, нужен отдельный провайдер) ---
    EMBED_BASE_URL: str = ""           # пусто -> берётся LLM_BASE_URL
    EMBED_API_KEY: str = ""            # пусто -> берётся LLM_API_KEY

    # --- Эмбеддинги ---
    EMBED_BACKEND: Literal["local", "api"] = "local"
    EMBED_MODEL: str = "BAAI/bge-m3"
    EMBED_DIM: int = 1024

    # --- БД ---
    DATABASE_URL: str = ""
    DB_POOL_SIZE: int = 10
    # Пулер Supabase в режиме транзакций (порт 6543) ломает подготовленные
    # выражения asyncpg. Определяется автоматически по строке подключения,
    # но можно включить принудительно.
    DB_DISABLE_STATEMENT_CACHE: bool = False

    # --- ASR (расшифровка голосовых) ---
    ASR_ENABLED: bool = True          # на слабом тарифе можно выключить (TZ шаг 13)
    # Откуда брать расшифровку голосовых:
    #   auto     — сначала силами Telegram (нужен Premium), иначе локальный whisper
    #   telegram — только силами Telegram, без запасного варианта
    #   local    — только локальный whisper
    ASR_PROVIDER: str = "auto"
    TELEGRAM_ASR_MAX_SEC: int = 180   # длиннее Telegram не расшифровывает
    TELEGRAM_ASR_WAIT_SEC: int = 90   # сколько ждать готовую расшифровку
    WHISPER_MODEL: str = "small"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    WHISPER_LANGUAGE: str = "ru"      # пусто -> автоопределение (дороже и ошибается)
    WHISPER_BEAM_SIZE: int = 5        # 1 быстрее примерно вдвое, качество чуть ниже
    WHISPER_CPU_THREADS: int = 0      # 0 -> по числу ядер
    MEDIA_CONCURRENCY: int = 2        # параллельных расшифровок (TZ шаг 5)
    MEDIA_QUEUE_MAXSIZE: int = 1000
    MEDIA_KEEP_FILES: bool = True     # false -> удалять аудио после расшифровки
    # Описание картинок мультимодальной моделью (TZ §4.1). Выключено: на активной
    # группе это сотни вызовов в день, а пользы меньше, чем от расшифровки голосовых.
    VISION_ENABLED: bool = False

    # --- Сеть / прочее ---
    PROXY_URL: str = ""
    TZ: str = "Europe/Moscow"  # сутки дайджеста и время рассылок
    DATA_DIR: Path = ROOT / "data"
    LOG_LEVEL: str = "INFO"
    # all — bot, web и collector в одном контейнере (src/supervisor.py);
    # остальные роли — по процессу на проект, если так удобнее масштабировать
    APP_ROLE: AppRole = "all"
    SERVICES: str = "bot,web,collector"  # что запускать при APP_ROLE=all
    # бот накатывает схему при старте; супервизор делает это сам до запуска всех
    # процессов и выключает повтор, чтобы ALTER не столкнулся с запросами коллектора
    MIGRATE_ON_START: bool = True

    # --- Веб-админка ---
    WEB_SECRET_KEY: str = ""
    WEB_BASE_URL: str = ""
    WEB_HOST: str = "0.0.0.0"
    WEB_PORT: int = 8080

    @field_validator("BOT_USERNAME", mode="before")
    @classmethod
    def _bare_username(cls, v: Any) -> Any:
        """Виджету входа нужно имя без «@» и без ссылки: @my_bot, t.me/my_bot → my_bot."""
        if isinstance(v, str):
            v = v.strip()
            for prefix in ("https://", "http://", "t.me/", "telegram.me/", "@"):
                if v.lower().startswith(prefix):
                    v = v[len(prefix):]
        return v

    @field_validator("TG_GROUP_ID", "OWNER_ID", "TG_API_ID", mode="before")
    @classmethod
    def _empty_int(cls, v: Any) -> Any:
        """Пустая строка в .env означает «не задано», а не ошибку валидации."""
        if isinstance(v, str) and not v.strip():
            return 0
        return v

    @property
    def llm_extra_body(self) -> dict[str, Any]:
        """Доп. поля запроса к LLM. Провайдеры называют их по-разному."""
        extra: dict[str, Any] = {}
        if self.LLM_THINKING == "disabled":
            extra["thinking"] = {"type": "disabled"}
        if self.LLM_REASONING_EFFORT:
            extra["reasoning_effort"] = self.LLM_REASONING_EFFORT
        return extra

    @property
    def embed_base_url(self) -> str:
        return self.EMBED_BASE_URL or self.LLM_BASE_URL

    @property
    def embed_api_key(self) -> str:
        return self.EMBED_API_KEY or self.LLM_API_KEY

    @property
    def media_dir(self) -> Path:
        return self.DATA_DIR / "media"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


cfg: Settings = get_settings()
