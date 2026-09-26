"""Тесты загрузки конфигурации (TZ шаг 1)."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import src.config as config_module


@pytest.fixture
def settings_cls():
    return config_module.Settings


def test_defaults_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls):
    """Без .env и переменных окружения конфиг грузится на дефолтах, а не падает."""
    for key in ("TG_API_ID", "OWNER_ID", "LLM_MODEL", "TZ", "EMBED_BACKEND", "APP_ROLE"):
        monkeypatch.delenv(key, raising=False)
    cfg = settings_cls(_env_file=tmp_path / "absent.env")

    assert cfg.TZ == "Europe/Moscow"
    assert cfg.LLM_MODEL == "deepseek-flash"
    assert cfg.EMBED_BACKEND == "local"
    assert cfg.EMBED_DIM == 1024
    assert cfg.APP_ROLE == "all"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls):
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("OWNER_ID", "777")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("EMBED_BACKEND", "api")
    cfg = settings_cls(_env_file=tmp_path / "absent.env")

    assert cfg.TG_API_ID == 12345
    assert cfg.OWNER_ID == 777
    assert cfg.LLM_MODEL == "deepseek-chat"
    assert cfg.EMBED_BACKEND == "api"


def test_empty_numeric_env_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls
):
    """В .env.example числовые ключи пустые — это «не задано», а не ошибка валидации."""
    monkeypatch.setenv("TG_API_ID", "")
    monkeypatch.setenv("OWNER_ID", "  ")
    monkeypatch.setenv("TG_GROUP_ID", "")
    cfg = settings_cls(_env_file=tmp_path / "absent.env")

    assert cfg.TG_API_ID == 0
    assert cfg.OWNER_ID == 0
    assert cfg.TG_GROUP_ID == 0


def test_llm_extra_body_disables_thinking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls
):
    """Замерено на deepseek-flash: с размышлениями 382 токена на ответ, без них 39."""
    monkeypatch.setenv("LLM_THINKING", "disabled")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    assert settings_cls(_env_file=tmp_path / "absent.env").llm_extra_body == {
        "thinking": {"type": "disabled"}
    }


def test_llm_extra_body_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls
):
    """Для провайдеров, где thinking не отключается (GLM), остаётся reasoning_effort."""
    monkeypatch.setenv("LLM_THINKING", "auto")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    assert settings_cls(_env_file=tmp_path / "absent.env").llm_extra_body == {
        "reasoning_effort": "low"
    }


def test_llm_extra_body_can_be_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls
):
    monkeypatch.setenv("LLM_THINKING", "auto")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    assert settings_cls(_env_file=tmp_path / "absent.env").llm_extra_body == {}


def test_embed_credentials_fall_back_to_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls
):
    """У DeepSeek эмбеддингов нет, но по умолчанию пусть берутся те же реквизиты."""
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("LLM_API_KEY", "ключ")
    monkeypatch.delenv("EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("EMBED_API_KEY", raising=False)
    cfg = settings_cls(_env_file=tmp_path / "absent.env")

    assert cfg.embed_base_url == "https://api.deepseek.com"
    assert cfg.embed_api_key == "ключ"

    monkeypatch.setenv("EMBED_BASE_URL", "https://embeddings.example.com")
    assert settings_cls(_env_file=tmp_path / "absent.env").embed_base_url == (
        "https://embeddings.example.com"
    )


def test_env_example_covers_all_settings(settings_cls):
    """Каждый ключ .env.example должен быть известен Settings — иначе тихо игнорируется."""
    example = Path("/".join([str(config_module.ROOT), ".env.example"])).read_text("utf-8")
    keys = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    unknown = keys - set(settings_cls.model_fields)
    assert not unknown, f"в .env.example есть неизвестные конфигу ключи: {unknown}"


def test_media_dir_under_data_dir(tmp_path: Path, settings_cls):
    cfg = settings_cls(_env_file=tmp_path / "absent.env", DATA_DIR=tmp_path)
    assert cfg.media_dir == tmp_path / "media"


def test_config_module_exposes_singleton():
    importlib.reload(config_module)
    assert config_module.cfg is config_module.get_settings()


def test_all_container_roles_are_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls
):
    """Точка входа образа умеет пять ролей — конфиг должен принимать каждую."""
    for role in ("all", "collector", "bot", "web", "migrate"):
        monkeypatch.setenv("APP_ROLE", role)
        assert settings_cls(_env_file=tmp_path / "absent.env").APP_ROLE == role


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://u:p@db.abc.supabase.co:5432/postgres", False),
        ("postgresql://u:p@aws-0-eu-west-2.pooler.supabase.com:5432/postgres", False),
        ("postgresql://u:p@aws-0-eu-west-2.pooler.supabase.com:6543/postgres", True),
        ("postgresql://u:p@host:5432/db?pgbouncer=true", True),
        ("", False),
    ],
)
def test_transaction_pooler_is_detected(dsn: str, expected: bool):
    """Через пулер на 6543 подготовленные выражения asyncpg ломаются."""
    from src.db.pool import uses_transaction_pooler

    assert uses_transaction_pooler(dsn) is expected


@pytest.mark.parametrize("raw", ["@temabot", " temabot ", "https://t.me/temabot", "t.me/temabot"])
def test_bot_username_is_normalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls, raw: str
):
    """Виджет входа говорит «Username invalid», если имя пришло с @ или ссылкой."""
    monkeypatch.setenv("BOT_USERNAME", raw)
    assert settings_cls(_env_file=tmp_path / "absent.env").BOT_USERNAME == "temabot"


def test_values_pasted_with_spaces_are_trimmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls
):
    monkeypatch.setenv("OWNER_ID", " 132036441 ")
    monkeypatch.setenv("LLM_MODEL", "deepseek-flash ")
    loaded = settings_cls(_env_file=tmp_path / "absent.env")

    assert loaded.OWNER_ID == 132036441 and loaded.LLM_MODEL == "deepseek-flash"
