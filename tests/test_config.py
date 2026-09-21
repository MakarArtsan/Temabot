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
    assert cfg.LLM_MODEL == "glm-5.3-flash"
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


def test_llm_extra_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings_cls):
    """reasoning_effort уезжает в extra_body, иначе GLM жжёт десятки тысяч токенов (TZ §1)."""
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    assert settings_cls(_env_file=tmp_path / "absent.env").llm_extra_body == {
        "reasoning_effort": "low"
    }

    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    assert settings_cls(_env_file=tmp_path / "absent.env").llm_extra_body == {}


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
