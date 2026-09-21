"""Тесты веб-админки (TZ §4.9, §9, шаг 12)."""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.config import cfg
from src.web import app as web_app
from src.web import auth

OWNER = 132036441
STRANGER = 999999
BOT_TOKEN = "123456:TEST-TOKEN"

def _request_with(raw: str) -> Any:
    """Минимальный Request с кукой сессии."""
    from fastapi import Request

    cookie = f"{auth.COOKIE_NAME}={raw}".encode()
    return Request({"type": "http", "headers": [(b"cookie", cookie)]})


CLOSED_PAGES = [
    "/", "/groups", "/selection", "/digests", "/learning", "/authors", "/qa", "/system",
]


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
    monkeypatch.setattr(cfg, "BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setattr(cfg, "OWNER_ID", OWNER)
    monkeypatch.setattr(cfg, "BOT_USERNAME", "temabot")
    monkeypatch.setattr(cfg, "WEB_BASE_URL", "https://admin.example.com")
    monkeypatch.setattr(cfg, "DATABASE_URL", "")


@pytest.fixture
def client() -> Any:
    # адрес по https: кука админки помечена Secure и по http не уедет
    return TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)


def signed(**overrides: Any) -> dict[str, Any]:
    """Данные виджета, подписанные так же, как это делает Telegram."""
    data: dict[str, Any] = {
        "id": OWNER, "first_name": "Владелец", "username": "owner",
        "auth_date": int(time.time()),
    }
    data.update(overrides)
    payload = {k: v for k, v in data.items() if k != "hash"}
    check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


# ============================================== доступ: главное требование §9

@pytest.mark.parametrize("path", CLOSED_PAGES)
def test_every_page_requires_login(client: Any, path: str):
    """В админке содержимое закрытых групп — публичных страниц быть не должно."""
    response = client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_page_is_public(client: Any):
    response = client.get("/login")
    assert response.status_code == 200
    assert "telegram-widget" in response.text


def test_export_requires_login(client: Any):
    assert client.get("/system/export/1").status_code == 303


def test_mutations_require_login(client: Any):
    response = client.post("/groups/-100111", data={"field": "collect", "value": "1"})
    assert response.status_code == 303


# ================================================ проверка подписи Telegram

def test_valid_signature_passes():
    assert auth.check_telegram_auth(signed(), bot_token=BOT_TOKEN) == OWNER


def test_tampered_field_is_rejected():
    """Подменить id в ссылке не выйдет — подпись перестанет сходиться."""
    data = signed()
    data["id"] = STRANGER

    with pytest.raises(auth.AuthError, match="не сходится"):
        auth.check_telegram_auth(data, bot_token=BOT_TOKEN)


def test_missing_hash_is_rejected():
    data = signed()
    del data["hash"]
    with pytest.raises(auth.AuthError, match="подпись"):
        auth.check_telegram_auth(data, bot_token=BOT_TOKEN)


def test_signature_from_another_bot_is_rejected():
    with pytest.raises(auth.AuthError):
        auth.check_telegram_auth(signed(), bot_token="999:OTHER")


def test_stale_login_is_rejected():
    """Перехваченную ссылку нельзя использовать через неделю."""
    old = signed(auth_date=int(time.time()) - 48 * 3600)
    with pytest.raises(auth.AuthError, match="устарели"):
        auth.check_telegram_auth(old, bot_token=BOT_TOKEN)


def test_auth_without_token_fails():
    with pytest.raises(auth.AuthError, match="BOT_TOKEN"):
        auth.check_telegram_auth(signed(), bot_token="")


# ====================================================== вход и сессия

def test_owner_gets_a_cookie(client: Any):
    response = client.get("/auth/telegram", params=signed())

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert auth.COOKIE_NAME in response.cookies


def test_cookie_is_httponly_and_secure(client: Any):
    response = client.get("/auth/telegram", params=signed())
    header = response.headers["set-cookie"]

    assert "HttpOnly" in header
    assert "Secure" in header, "адрес админки https — кука не должна ходить по http"


def test_stranger_cannot_get_in(client: Any, monkeypatch: pytest.MonkeyPatch):
    """Подпись верная, но это не владелец."""
    data = signed(id=STRANGER)
    # пересоберём подпись под чужой id — Telegram подписал бы честно
    payload = {k: v for k, v in data.items() if k != "hash"}
    check = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

    response = client.get("/auth/telegram", params=data)

    assert response.status_code == 303
    assert "/login" in response.headers["location"]
    assert auth.COOKIE_NAME not in response.cookies


def test_forged_cookie_is_rejected():
    assert auth.read_session("совершенно.поддельная.кука") is None
    assert auth.read_session(None) is None


def test_session_survives_a_roundtrip():
    session = auth.read_session(auth.issue_session(OWNER))
    assert session is not None and session["uid"] == OWNER
    assert session["csrf"], "CSRF-токен выдаётся вместе с сессией"


def test_cookie_of_former_owner_stops_working(monkeypatch: pytest.MonkeyPatch):
    """Сменили OWNER_ID в конфиге — старая кука больше не годится."""
    raw = auth.issue_session(OWNER)
    monkeypatch.setattr(cfg, "OWNER_ID", 555)

    request = _request_with(raw)
    assert auth.current_user(request) is None


def test_logout_clears_the_cookie(client: Any):
    client.get("/auth/telegram", params=signed())
    response = client.get("/logout")

    assert response.status_code == 303
    assert 'tgd_session=""' in response.headers["set-cookie"]


# ============================================================== CSRF

def test_csrf_is_required(monkeypatch: pytest.MonkeyPatch):
    from fastapi import HTTPException

    raw = auth.issue_session(OWNER)
    request = _request_with(raw)
    good = auth.read_session(raw)["csrf"]

    auth.check_csrf(request, good)   # не бросает

    for bad in ("", None, "чужой-токен"):
        with pytest.raises(HTTPException) as exc:
            auth.check_csrf(request, bad)
        assert exc.value.status_code == 403


def test_mutation_without_csrf_is_refused(client: Any, monkeypatch: pytest.MonkeyPatch):
    async def set_chat_flags(*a: Any, **kw: Any) -> Any:
        raise AssertionError("не должно вызваться без CSRF")

    monkeypatch.setattr(web_app.repo, "set_chat_flags", set_chat_flags)
    client.get("/auth/telegram", params=signed())

    response = client.post(
        "/groups/-100111", data={"field": "collect", "value": "1", "csrf_token": "мимо"}
    )
    assert response.status_code == 403


# ====================================================== страницы владельца

@pytest.fixture
def owner_client(client: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Клиент с валидной сессией и подменённой БД."""
    async def list_chats(**kw: Any) -> list[Any]:
        from src.db.models import Chat

        return [Chat(id=1, tg_id=-100111, title="Рабочая", collect=True, copier="allow")]

    async def stats() -> dict[str, Any]:
        return {
            "messages": 42, "authors": 3, "voices": 2, "transcribed": 1,
            "last_at": None, "tokens_in": 1000, "tokens_out": 200, "llm_calls": 5,
            "digests": 1,
        }

    async def empty_list(*a: Any, **kw: Any) -> list[Any]:
        return []

    async def zero(*a: Any, **kw: Any) -> int:
        return 0

    async def empty_dict(*a: Any, **kw: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(web_app.repo, "list_chats", list_chats)
    monkeypatch.setattr(web_app.repo, "get_collection_stats", stats)
    monkeypatch.setattr(web_app.repo, "messages_per_day", empty_list)
    monkeypatch.setattr(web_app.repo, "tokens_per_day", empty_list)
    monkeypatch.setattr(web_app.repo, "list_recent_digests", empty_list)
    monkeypatch.setattr(web_app.repo, "list_authors", empty_list)
    monkeypatch.setattr(web_app.repo, "list_qa_log", empty_list)
    monkeypatch.setattr(web_app.repo, "get_states", empty_dict)
    monkeypatch.setattr(web_app.repo, "pending_media_count", zero)
    monkeypatch.setattr(web_app.repo, "count_feedback", zero)

    client.get("/auth/telegram", params=signed())
    return client


@pytest.mark.parametrize("path", CLOSED_PAGES)
def test_owner_sees_every_page(owner_client: Any, path: str):
    response = owner_client.get(path)
    assert response.status_code == 200, path


def test_groups_page_shows_toggles(owner_client: Any):
    text = owner_client.get("/groups").text

    assert "Рабочая" in text
    assert "сбор включён" in text
    assert "hx-post=\"/groups/-100111\"" in text


def test_selection_page_shows_weights(owner_client: Any):
    text = owner_client.get("/selection").text

    assert "Профиль интересов" in text
    assert "w_use" in text and "повтор вчерашней темы" in text
    assert "Прогнать на вчера" in text


def test_dashboard_shows_numbers(owner_client: Any):
    text = owner_client.get("/").text
    assert "42" in text and "Очередь расшифровки" in text


# ====================================================== проверка живости

def test_healthz_is_public_but_tells_nothing(client: Any):
    """Amvera и docker должны видеть, что процесс жив, не заходя в админку."""
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "database"}, "ничего лишнего наружу"


def test_healthz_reports_database_trouble(client: Any, monkeypatch: pytest.MonkeyPatch):
    async def broken(*a: Any, **kw: Any) -> Any:
        raise ConnectionError("база недоступна")

    monkeypatch.setattr(cfg, "DATABASE_URL", "postgresql://nope")
    monkeypatch.setattr(web_app.pool, "fetchval", broken)

    assert client.get("/healthz").json()["database"] == "fail"
