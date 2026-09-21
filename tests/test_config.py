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
    for key in ("TG_API_ID", "OWNER_ID", "LLM_MODEL", "TZ", "EMBED_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    cfg = settings_cls(_env_file=tmp_path / "absent.env")

    assert cfg.TZ == "Asia/Kamchatka"
    assert cfg.LLM_MODEL == "deepseek-flash"
    assert cfg.EMBED_BACKEND == "local"
    assert cfg.EMBED_DIM == 1024
    assert cfg.APP_ROLE == "bot"


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
