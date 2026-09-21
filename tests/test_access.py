"""Тесты доступа к группам и блок-листа (TZ §4.8, шаг 10)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.bot import handlers_admin as admin
from src.bot import handlers_qa
from src.bot.middlewares import CopierAccess, CopierRateLimit, SettingsCache
from src.db.models import Chat

OWNER = 132036441
STRANGER = 999999
GROUP = -1002354231333


def chat(copier: str = "ask", **kw: Any) -> Chat:
    defaults: dict[str, Any] = dict(id=1, tg_id=GROUP, title="Группа", copier=copier)
    defaults.update(kw)
    return Chat(**defaults)


class FakeCache(SettingsCache):
    """Кэш с заранее известным содержимым — без обращения к БД."""

    def __init__(self, chats: dict[int, Chat] | None = None, blocked: set[int] | None = None):
        super().__init__()
        self._fake_chats = chats or {}
        self._fake_blocked = blocked or set()

    async def chat(self, chat_tg_id: int, *, now: float | None = None) -> Chat | None:
        return self._fake_chats.get(chat_tg_id)

    async def blocked(self, *, now: float | None = None) -> set[int]:
        return self._fake_blocked


# --------------------------------------------------------- доступ копировщика

@pytest.mark.parametrize(
    ("mode", "allowed"),
    [("allow", True), ("deny", False), ("ask", False)],
)
async def test_copier_works_only_in_allowed_groups(mode: str, allowed: bool):
    """Пока владелец не решил (ask), бот в группе молчит."""
    access = CopierAccess(FakeCache({GROUP: chat(mode)}), owner_id=OWNER)
    assert await access.allowed(GROUP, "supergroup", STRANGER) is allowed


async def test_unknown_group_is_not_allowed():
    access = CopierAccess(FakeCache({}), owner_id=OWNER)
    assert await access.allowed(-100777, "supergroup", OWNER) is False


async def test_blocked_user_is_ignored_even_in_allowed_group():
    access = CopierAccess(FakeCache({GROUP: chat("allow")}, blocked={STRANGER}), owner_id=OWNER)

    assert await access.allowed(GROUP, "supergroup", STRANGER) is False
    assert await access.allowed(GROUP, "supergroup", OWNER) is True


async def test_in_private_only_owner():
    """Иначе любой желающий получил бы бесплатный Telegraph-постер от имени бота."""
    access = CopierAccess(FakeCache({}), owner_id=OWNER)

    assert await access.allowed(OWNER, "private", OWNER) is True
    assert await access.allowed(STRANGER, "private", STRANGER) is False


async def test_middleware_stops_the_handler():
    access = CopierAccess(FakeCache({GROUP: chat("deny")}), owner_id=OWNER)
    called = False

    async def handler(event: Any, data: dict[str, Any]) -> str:
        nonlocal called
        called = True
        return "ok"

    from aiogram.types import Chat as TgChat, Message as TgMessage

    message = TgMessage.model_construct(
        message_id=1, chat=TgChat(id=GROUP, type="supergroup"), text="@bot текст"
    )
    result = await access(handler, message, {"event_from_user": SimpleNamespace(id=STRANGER)})

    assert result is None and called is False


# ----------------------------------------------------------------- лимит

def test_copier_rate_limit_is_ten_per_minute():
    limiter = CopierRateLimit()
    allowed = [limiter.allow(STRANGER, now=0.0) for _ in range(12)]

    assert sum(allowed) == 10
    assert allowed[10] is False


async def test_rate_limited_user_gets_no_answer_in_group():
    """Предупреждение о лимите в группе — это тоже флуд."""
    limiter = CopierRateLimit()
    for _ in range(10):
        limiter.allow(STRANGER)

    async def handler(event: Any, data: dict[str, Any]) -> str:
        raise AssertionError("не должно вызваться")

    result = await limiter(handler, object(), {"event_from_user": SimpleNamespace(id=STRANGER)})
    assert result is None


# ------------------------------------------------------- кэш настроек

async def test_cache_reads_database_once_per_minute(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    async def get_chat_by_tg_id(chat_tg_id: int) -> Chat:
        calls["n"] += 1
        return chat("allow")

    monkeypatch.setattr(admin.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    from src.bot import middlewares

    monkeypatch.setattr(middlewares.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    cache = SettingsCache(ttl_sec=60)

    await cache.chat(GROUP, now=0.0)
    await cache.chat(GROUP, now=30.0)
    assert calls["n"] == 1, "внутри минуты берём из кэша"

    await cache.chat(GROUP, now=61.0)
    assert calls["n"] == 2, "через минуту перечитываем — перезапуск не нужен"


async def test_cache_can_be_dropped_immediately(monkeypatch: pytest.MonkeyPatch):
    """После переключения флага из бота ждать минуту глупо."""
    calls = {"n": 0}

    async def get_chat_by_tg_id(chat_tg_id: int) -> Chat:
        calls["n"] += 1
        return chat("allow")

    from src.bot import middlewares

    monkeypatch.setattr(middlewares.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    cache = SettingsCache(ttl_sec=60)

    await cache.chat(GROUP, now=0.0)
    cache.forget(GROUP)
    await cache.chat(GROUP, now=1.0)

    assert calls["n"] == 2


# ------------------------------------------------- добавление бота в группу

class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.left: list[int] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, **kw})

    async def leave_chat(self, chat_id: int) -> None:
        self.left.append(chat_id)


def added_event(title: str = "Новая группа", chat_id: int = -100555) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, title=title, type="supergroup"),
        from_user=SimpleNamespace(id=STRANGER, username="vasya", full_name="Вася"),
    )


@pytest.fixture
def owner_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin.cfg, "OWNER_ID", OWNER)


async def test_added_to_denied_group_leaves_immediately(
    monkeypatch: pytest.MonkeyPatch, owner_id
):
    """Главная проверка ТЗ: бота добавили в группу со статусом deny — выходит сам."""
    async def get_or_create_chat(chat_tg_id: int, title: str | None = None) -> Chat:
        return chat("deny", tg_id=chat_tg_id, title=title)

    monkeypatch.setattr(admin.repo, "get_or_create_chat", get_or_create_chat)
    bot = FakeBot()

    await admin.on_added_to_chat(added_event(), bot)

    assert bot.left == [-100555], "должен выйти без вопросов"
    assert bot.sent and bot.sent[0]["chat_id"] == OWNER
    assert "вышел" in bot.sent[0]["text"]


async def test_added_to_new_group_asks_the_owner(monkeypatch: pytest.MonkeyPatch, owner_id):
    async def get_or_create_chat(chat_tg_id: int, title: str | None = None) -> Chat:
        return chat("ask", tg_id=chat_tg_id, title=title)

    monkeypatch.setattr(admin.repo, "get_or_create_chat", get_or_create_chat)
    bot = FakeBot()

    await admin.on_added_to_chat(added_event("Чужая группа"), bot)

    assert bot.left == [], "до решения не выходим, но и не работаем"
    message = bot.sent[0]
    assert message["chat_id"] == OWNER, "спрашиваем только владельца"
    assert "Чужая группа" in message["text"]
    assert "@vasya" in message["text"], "видно, кто добавил"
    assert "молчу" in message["text"]

    buttons = [b.text for row in message["reply_markup"].inline_keyboard for b in row]
    assert buttons == ["✅ Разрешить", "🚫 Запретить и выйти"]


async def test_added_to_allowed_group_just_works(monkeypatch: pytest.MonkeyPatch, owner_id):
    async def get_or_create_chat(chat_tg_id: int, title: str | None = None) -> Chat:
        return chat("allow", tg_id=chat_tg_id, title=title)

    monkeypatch.setattr(admin.repo, "get_or_create_chat", get_or_create_chat)
    bot = FakeBot()

    await admin.on_added_to_chat(added_event(), bot)

    assert bot.left == []
    assert "работаю" in bot.sent[0]["text"]


async def test_private_chat_start_is_not_a_group_addition(
    monkeypatch: pytest.MonkeyPatch, owner_id
):
    """Когда пользователь просто нажал /start, это не «добавили в группу»."""
    bot = FakeBot()
    event = SimpleNamespace(
        chat=SimpleNamespace(id=OWNER, title=None, type="private"),
        from_user=SimpleNamespace(id=OWNER, username=None, full_name="Владелец"),
    )
    await admin.on_added_to_chat(event, bot)

    assert bot.sent == [] and bot.left == []


# ------------------------------------------------------------ решение по кнопке

class FakeCallback:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.message = None

    async def answer(self, text: str | None = None, **kw: Any) -> None:
        self.answers.append(text or "")


async def test_deny_button_leaves_the_group(monkeypatch: pytest.MonkeyPatch):
    flags: list[dict[str, Any]] = []

    async def get_chat_by_tg_id(chat_tg_id: int) -> Chat:
        return chat("ask")

    async def set_chat_flags(chat_tg_id: int, **kw: Any) -> Chat:
        flags.append(kw)
        return chat("deny")

    monkeypatch.setattr(admin.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    monkeypatch.setattr(admin.repo, "set_chat_flags", set_chat_flags)
    bot = FakeBot()
    callback = FakeCallback()

    await admin.on_group_decision(callback, admin.GroupCb(action="deny", chat_id=GROUP), bot)

    assert flags == [{"copier": "deny"}]
    assert bot.left == [GROUP]
    assert "вышел" in callback.answers[0]


async def test_allow_button_keeps_the_bot(monkeypatch: pytest.MonkeyPatch):
    flags: list[dict[str, Any]] = []

    async def get_chat_by_tg_id(chat_tg_id: int) -> Chat:
        return chat("ask")

    async def set_chat_flags(chat_tg_id: int, **kw: Any) -> Chat:
        flags.append(kw)
        return chat("allow")

    monkeypatch.setattr(admin.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    monkeypatch.setattr(admin.repo, "set_chat_flags", set_chat_flags)
    bot = FakeBot()
    callback = FakeCallback()

    await admin.on_group_decision(callback, admin.GroupCb(action="allow", chat_id=GROUP), bot)

    assert flags == [{"copier": "allow"}]
    assert bot.left == []


async def test_toggles_flip_the_flag(monkeypatch: pytest.MonkeyPatch):
    flags: list[dict[str, Any]] = []

    async def get_chat_by_tg_id(chat_tg_id: int) -> Chat:
        return chat("allow", collect=False, digest=True)

    async def set_chat_flags(chat_tg_id: int, **kw: Any) -> Chat:
        flags.append(kw)
        return chat("allow")

    monkeypatch.setattr(admin.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    monkeypatch.setattr(admin.repo, "set_chat_flags", set_chat_flags)

    await admin.on_group_decision(
        FakeCallback(), admin.GroupCb(action="collect", chat_id=GROUP), FakeBot()
    )
    await admin.on_group_decision(
        FakeCallback(), admin.GroupCb(action="digest", chat_id=GROUP), FakeBot()
    )

    assert flags == [{"collect": True}, {"digest": False}]


# ------------------------------------------------------------ /ask #группа

async def test_group_filter_is_parsed(monkeypatch: pytest.MonkeyPatch):
    async def find_chats(query: str) -> list[Chat]:
        return [chat("allow", title="Рабочая")]

    monkeypatch.setattr(handlers_qa.repo, "find_chats", find_chats)

    question, group, error = await handlers_qa.split_group_filter("#рабочая когда конференция")

    assert question == "когда конференция"
    assert group is not None and group.title == "Рабочая"
    assert error is None


async def test_question_without_group_filter():
    question, group, error = await handlers_qa.split_group_filter("когда конференция")
    assert (question, group, error) == ("когда конференция", None, None)


async def test_unknown_group_is_reported(monkeypatch: pytest.MonkeyPatch):
    async def find_chats(query: str) -> list[Chat]:
        return []

    monkeypatch.setattr(handlers_qa.repo, "find_chats", find_chats)

    _, group, error = await handlers_qa.split_group_filter("#несуществующая вопрос")

    assert group is None
    assert error is not None and "не знаю" in error


async def test_ambiguous_group_is_reported(monkeypatch: pytest.MonkeyPatch):
    async def find_chats(query: str) -> list[Chat]:
        return [chat(title="Рабочая одна"), chat(title="Рабочая две")]

    monkeypatch.setattr(handlers_qa.repo, "find_chats", find_chats)

    _, group, error = await handlers_qa.split_group_filter("#рабочая вопрос")

    assert group is None
    assert error is not None and "несколько" in error
