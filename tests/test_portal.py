"""Страница участников: доступ только участникам группы и только чтение (TZ §9)."""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.config import cfg
from src.db.models import Chat, Digest
from src.digest.render import DigestData, Topic
from src.web import app as web_app
from src.web import auth, membership

OWNER = 132036441
MEMBER = 555001
STRANGER = 555002
BOT_TOKEN = "123456:TEST-TOKEN"
DAY = date(2026, 9, 24)

ADMIN_PAGES = [
    "/", "/groups", "/selection", "/digests", "/digests/5", "/learning", "/authors",
    "/ratings", "/qa", "/system", "/system/export/1",
]
PORTAL_PAGES = ["/g", "/g/1", f"/g/1/d/{DAY.isoformat()}", "/g/1/ratings"]


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
    monkeypatch.setattr(cfg, "BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setattr(cfg, "OWNER_ID", OWNER)
    monkeypatch.setattr(cfg, "BOT_USERNAME", "temabot")
    monkeypatch.setattr(cfg, "WEB_BASE_URL", "https://admin.example.com")
    monkeypatch.setattr(cfg, "DATABASE_URL", "")
    membership.forget()


def signed(user_id: int) -> dict[str, Any]:
    """Данные виджета, подписанные так же, как это делает Telegram."""
    data: dict[str, Any] = {"id": user_id, "first_name": "Кто-то", "auth_date": int(time.time())}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


def client_as(user_id: int | None) -> TestClient:
    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    if user_id is not None:
        client.cookies.set(auth.COOKIE_NAME, auth.issue_session(user_id))
    return client


def _payload(**kw: Any) -> dict[str, Any]:
    data = DigestData(
        chat_tg_id=-100111, day=DAY, chat_title="",
        highlights=["Veo 4 подешевел до 12 ₽ за ролик"],
        topics=[Topic(thread_id=11, title="Цены <script>на генерацию</script>", kind="insight",
                      takeaway="12 ₽ за ролик", msg_count=23, participants=["Вася", "Петя"],
                      key_msg_ids=[4410],
                      features={"usefulness": 0.8, "секретный_признак": 0.99})],
        unanswered=[("Кто пробовал Topaz?", 4471)],
        links=["https://example.com/stock", "javascript:alert(1)"],
        msg_count=86, participants=14,
        heroes="🏅 Герои дня: 🧠 Петя",
        **kw,
    )
    return data.to_dict()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Одна группа, её дайджест и статистика; режим и членство меняются в тестах."""
    state: dict[str, Any] = {"portal": "all", "members": {MEMBER}, "stats_calls": []}

    def chat() -> Chat:
        return Chat(id=1, tg_id=-100111, title="Нейросети и видео", collect=True, digest=True,
                    portal=state["portal"])

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return chat() if chat_id == 1 else None

    async def list_chats(**kw: Any) -> list[Chat]:
        return [chat()]

    async def list_portal_chats() -> list[Chat]:
        return [chat()] if state["portal"] != "off" else []

    digest = Digest(id=5, chat_id=1, day=DAY, summary_md="", payload=_payload(), msg_count=86)

    async def list_chat_digests(chat_id: int, limit: int = 60) -> list[Digest]:
        return [digest]

    async def get_digest(chat_id: int, day: date) -> Digest | None:
        return digest if day == DAY else None

    async def messages_per_day(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"day": DAY, "count": 86}]

    async def get_author_stats(chat_id: Any, **kw: Any) -> list[dict[str, Any]]:
        state["stats_calls"].append(kw)
        row = {"messages": 10, "short_msgs": 0, "words": 100, "longest_msg": 10, "voice_sec": 0,
               "links": 1, "replies_got": 5, "reactions_got": 3, "questions_answered": 1,
               "threads_started": 1, "night_msgs": 7, "usefulness": 1.2}
        return [
            {**row, "tg_user_id": 1, "name": "Петя"},
            {**row, "tg_user_id": 2, "name": "Маша", "usefulness": 0.4, "night_msgs": 9},
        ]

    async def is_member(chat: Chat, user_id: int, **kw: Any) -> bool:
        return user_id == OWNER or user_id in state["members"]

    for name, fn in {
        "get_chat_by_id": get_chat_by_id, "list_chats": list_chats,
        "list_portal_chats": list_portal_chats, "list_chat_digests": list_chat_digests,
        "get_digest": get_digest, "messages_per_day": messages_per_day,
        "get_author_stats": get_author_stats,
    }.items():
        monkeypatch.setattr(web_app.repo, name, fn)
    monkeypatch.setattr(membership, "is_member", is_member)
    return state


# ============================================ посторонним и участникам — не админка

@pytest.mark.parametrize("path", PORTAL_PAGES)
def test_portal_requires_login(path: str):
    response = client_as(None).get(path)
    assert response.status_code == 303 and response.headers["location"] == "/login"


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_member_cookie_does_not_open_the_admin(world: dict[str, Any], path: str):
    """Участник вошёл честно, но админка — только для OWNER_ID."""
    response = client_as(MEMBER).get(path)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_member_cannot_change_anything(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    async def explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("участник не может ничего менять")

    for name in ("set_chat_flags", "update_chat_settings", "add_feedback", "set_author_flags",
                 "block_user"):
        monkeypatch.setattr(web_app.repo, name, explode)

    client = client_as(MEMBER)
    csrf = auth.read_session(client.cookies.get(auth.COOKIE_NAME))["csrf"]
    attempts = [
        ("/groups/-100111", {"field": "portal", "value": "off"}),
        ("/selection/1", {"interests_profile": "взлом"}),
        ("/feedback/1", {"value": "1"}),
        ("/authors/7", {"field": "muted", "value": "1"}),
        ("/digests/5/publish", {}),
        ("/system/reindex", {}),
    ]
    for path, data in attempts:
        response = client.post(path, data={**data, "csrf_token": csrf})
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path


def test_member_session_is_not_an_owner_session():
    from fastapi import Request

    cookie = f"{auth.COOKIE_NAME}={auth.issue_session(MEMBER)}".encode()
    request = Request({"type": "http", "headers": [(b"cookie", cookie)]})

    assert auth.current_viewer(request) is not None
    assert auth.current_user(request) is None


def test_member_on_login_page_goes_to_portal(world: dict[str, Any]):
    response = client_as(MEMBER).get("/login")
    assert response.status_code == 303 and response.headers["location"] == "/g"


# ================================================================== вход

def test_member_logs_in_to_the_portal(world: dict[str, Any]):
    response = client_as(None).get("/auth/telegram", params=signed(MEMBER))

    assert response.status_code == 303
    assert response.headers["location"] == "/g"
    assert auth.COOKIE_NAME in response.cookies


def test_stranger_gets_no_cookie(world: dict[str, Any]):
    response = client_as(None).get("/auth/telegram", params=signed(STRANGER))

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?error=")
    assert auth.COOKIE_NAME not in response.cookies


def test_nobody_gets_in_while_the_portal_is_closed(world: dict[str, Any]):
    world["portal"] = "off"
    response = client_as(None).get("/auth/telegram", params=signed(MEMBER))

    assert auth.COOKIE_NAME not in response.cookies


def test_login_fails_closed_when_access_cannot_be_checked(monkeypatch: pytest.MonkeyPatch):
    async def broken() -> list[Chat]:
        raise ConnectionError("база недоступна")

    monkeypatch.setattr(web_app.repo, "list_portal_chats", broken)
    response = client_as(None).get("/auth/telegram", params=signed(MEMBER))

    assert response.status_code == 303 and "/login?error=" in response.headers["location"]
    assert auth.COOKIE_NAME not in response.cookies


# ============================================================ кто что видит

def test_member_sees_the_digest(world: dict[str, Any]):
    client = client_as(MEMBER)

    home = client.get("/g")
    assert home.status_code == 303, "одна группа — сразу в неё"
    assert home.headers["location"] == "/g/1"

    page = client.get("/g/1")
    assert page.status_code == 200
    assert "Нейросети и видео" in page.text and "Veo 4 подешевел" in page.text

    day = client.get(f"/g/1/d/{DAY.isoformat()}")
    assert day.status_code == 200
    assert "12 ₽ за ролик" in day.text


def test_digest_page_is_read_only_and_escaped(world: dict[str, Any]):
    text = client_as(MEMBER).get(f"/g/1/d/{DAY.isoformat()}").text

    assert "<script>на генерацию</script>" not in text
    assert "&lt;script&gt;на генерацию&lt;/script&gt;" in text
    # ни оценок, ни признаков, ни кнопок обучения, ни форм
    for leak in ("секретный_признак", "Оценка", "порог", "/feedback/", "csrf_token", "<form"):
        assert leak not in text, leak
    # ссылка вида javascript: не становится кликабельной
    assert 'href="javascript:' not in text
    assert 'href="https://example.com/stock"' in text


def test_heroes_only_when_ratings_are_open(world: dict[str, Any]):
    client = client_as(MEMBER)
    assert "Герои дня" in client.get(f"/g/1/d/{DAY.isoformat()}").text

    world["portal"] = "digests"
    assert "Герои дня" not in client.get(f"/g/1/d/{DAY.isoformat()}").text


def test_ratings_show_only_public_nominations(world: dict[str, Any]):
    response = client_as(MEMBER).get("/g/1/ratings")

    assert response.status_code == 200
    assert "Самый полезный" in response.text and "Петя" in response.text
    assert "Сова" not in response.text and "Цепляет" not in response.text
    # скрывшиеся по /optout отфильтровывает запрос
    assert world["stats_calls"] and all(c["hide_optout"] is True for c in world["stats_calls"])


def test_ratings_are_closed_in_digests_mode(world: dict[str, Any]):
    world["portal"] = "digests"
    client = client_as(MEMBER)

    assert client.get("/g/1/ratings").status_code == 404
    assert "/g/1/ratings" not in client.get("/g/1").text, "и ссылки на них нет"


@pytest.mark.parametrize("path", PORTAL_PAGES[1:])
def test_closed_portal_is_a_404(world: dict[str, Any], path: str):
    world["portal"] = "off"
    response = client_as(MEMBER).get(path)

    assert response.status_code == 404
    assert "Нейросети и видео" not in response.text, "название группы не раскрываем"


@pytest.mark.parametrize("path", PORTAL_PAGES[1:])
def test_non_member_is_a_404(world: dict[str, Any], path: str):
    response = client_as(STRANGER).get(path)

    assert response.status_code == 404
    assert "Нейросети и видео" not in response.text and "Veo 4" not in response.text


def test_member_who_left_loses_access(world: dict[str, Any]):
    client = client_as(MEMBER)
    assert client.get("/g/1").status_code == 200

    world["members"].clear()
    assert client.get("/g/1").status_code == 404


def test_unknown_group_and_day_are_404(world: dict[str, Any]):
    client = client_as(MEMBER)
    assert client.get("/g/99").status_code == 404
    assert client.get("/g/1/d/2020-01-01").status_code == 404
    assert client.get("/g/1/d/вчера").status_code == 404


def test_owner_can_preview_a_closed_portal(world: dict[str, Any]):
    world["portal"] = "off"
    page = client_as(OWNER).get("/g/1")

    assert page.status_code == 200
    assert "закрыта" in page.text


def test_pages_are_not_cached_or_framed(world: dict[str, Any]):
    response = client_as(MEMBER).get("/g/1")

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"


def test_static_files_are_public_but_pages_are_not():
    client = client_as(None)
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/vendor/chart.umd.min.js").status_code == 200
    assert client.get("/static/../app.py").status_code == 404


# ================================================== режим из админки

def test_owner_switches_portal_mode(monkeypatch: pytest.MonkeyPatch):
    saved: list[dict[str, Any]] = []
    forgotten: list[int | None] = []

    async def set_chat_flags(chat_tg_id: int, **kw: Any) -> Chat:
        saved.append(kw)
        return Chat(id=1, tg_id=chat_tg_id, title="Нейросети и видео", portal=kw["portal"])

    async def get_chat_by_tg_id(chat_tg_id: int) -> Chat:
        return Chat(id=1, tg_id=chat_tg_id, title="Нейросети и видео", portal="digests")

    monkeypatch.setattr(web_app.repo, "set_chat_flags", set_chat_flags)
    monkeypatch.setattr(web_app.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    monkeypatch.setattr(membership, "forget", lambda chat_id=None: forgotten.append(chat_id))

    client = client_as(OWNER)
    csrf = auth.read_session(client.cookies.get(auth.COOKIE_NAME))["csrf"]
    ok = client.post(
        "/groups/-100111", data={"field": "portal", "value": "digests", "csrf_token": csrf}
    )
    bad = client.post(
        "/groups/-100111", data={"field": "portal", "value": "всем", "csrf_token": csrf}
    )

    assert ok.status_code == 200 and saved == [{"portal": "digests"}]
    assert forgotten == [1], "запомненные ответы о членстве сбрасываются"
    assert "https://admin.example.com/g" in ok.text, "ссылку для группы видно сразу"
    assert bad.status_code == 400


# ============================================================ проверка членства

class FakeBot:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0
        self.session = SimpleNamespace(close=self._close)
        self.closed = False

    async def _close(self) -> None:
        self.closed = True

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def send_message(self, *a: Any, **kw: Any) -> None:
        raise AssertionError("проверка членства ничего не отправляет")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (SimpleNamespace(status="member"), "member"),
        (SimpleNamespace(status="administrator"), "member"),
        (SimpleNamespace(status="creator"), "member"),
        (SimpleNamespace(status="restricted", is_member=True), "member"),
        (SimpleNamespace(status="restricted", is_member=False), "left"),
        (SimpleNamespace(status="left"), "left"),
        (SimpleNamespace(status="kicked"), "kicked"),
        (RuntimeError("Bad Request: chat not found"), None),
    ],
)
async def test_telegram_status(monkeypatch: pytest.MonkeyPatch, answer: Any, expected: str | None):
    bot = FakeBot(answer)
    monkeypatch.setattr(membership, "make_bot", lambda: bot)

    assert await membership.telegram_status(-100111, MEMBER) == expected
    assert bot.closed, "сессию бота закрываем всегда"


@pytest.fixture
def decide(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Подменить оба источника: ответ Telegram и список коллектора."""
    sources: dict[str, Any] = {"telegram": None, "collector": None, "asked": 0}

    async def telegram_status(chat_tg_id: int, user_id: int) -> str | None:
        sources["asked"] += 1
        return sources["telegram"]

    async def chat_member_status(chat_id: int, user_id: int, **kw: Any) -> bool | None:
        return sources["collector"]

    monkeypatch.setattr(membership, "telegram_status", telegram_status)
    monkeypatch.setattr(membership.repo, "chat_member_status", chat_member_status)
    return sources


CHAT = Chat(id=1, tg_id=-100111, portal="all")


@pytest.mark.parametrize(
    ("telegram", "collector", "allowed"),
    [
        ("member", None, True),        # Telegram подтвердил
        ("member", False, True),       # живой ответ важнее списка
        ("kicked", True, False),       # удалённого не пускаем, что бы ни было в списке
        ("left", True, True),          # бот видит не всех — решает свежий список
        ("left", False, False),
        ("left", None, False),
        (None, True, True),            # Telegram недоступен — свежий список
        (None, None, False),           # ничего не известно — не пускаем
    ],
)
async def test_membership_decision(decide: dict[str, Any], telegram: Any, collector: Any,
                                   allowed: bool):
    decide.update(telegram=telegram, collector=collector)
    assert await membership.is_member(CHAT, MEMBER) is allowed


async def test_owner_is_always_a_member(decide: dict[str, Any]):
    assert await membership.is_member(CHAT, OWNER) is True
    assert decide["asked"] == 0


async def test_answers_are_cached_briefly(decide: dict[str, Any]):
    decide["telegram"] = "member"
    assert await membership.is_member(CHAT, MEMBER, now=1000.0)
    decide["telegram"] = "kicked"

    assert await membership.is_member(CHAT, MEMBER, now=1000.0 + 60), "в пределах кэша"
    assert decide["asked"] == 1
    assert not await membership.is_member(CHAT, MEMBER, now=1000.0 + membership.ALLOW_TTL_SEC + 1)


async def test_visible_chats(monkeypatch: pytest.MonkeyPatch, decide: dict[str, Any]):
    open_chat = Chat(id=1, tg_id=-100111, portal="digests")
    closed_chat = Chat(id=2, tg_id=-100222, portal="off")

    async def list_chats(**kw: Any) -> list[Chat]:
        return [open_chat, closed_chat]

    async def list_portal_chats() -> list[Chat]:
        return [open_chat]

    monkeypatch.setattr(membership.repo, "list_chats", list_chats)
    monkeypatch.setattr(membership.repo, "list_portal_chats", list_portal_chats)
    decide["telegram"] = "member"

    assert await membership.visible_chats(OWNER) == [open_chat, closed_chat], "владелец видит всё"
    assert await membership.visible_chats(MEMBER) == [open_chat]
    decide["telegram"] = "kicked"
    membership.forget()
    assert await membership.visible_chats(MEMBER) == []


# ============================================================ мелочи отображения

def test_chat_name_never_shows_a_bare_id():
    assert web_app.chat_name(Chat(id=1, tg_id=-100111, title="Нейросети")) == "Нейросети"
    assert web_app.chat_name(Chat(id=1, tg_id=-100111)) == "Группа -100111"
    assert web_app.chat_name({"title": None, "tg_id": -100222}) == "Группа -100222"


@pytest.mark.parametrize(
    ("n", "word"), [(1, "тема"), (2, "темы"), (5, "тем"), (11, "тем"), (21, "тема"), (104, "темы")]
)
def test_plural(n: int, word: str):
    assert web_app.plural(n, "тема", "темы", "тем") == word
