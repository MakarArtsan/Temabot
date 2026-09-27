"""Публикация в тему форума, сообщения ботов вне дайджеста, выбор темы в админке."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from src.collector import topics as forum
from src.config import cfg
from src.db import repo
from src.db.models import Chat, Digest, Message
from src.digest import publish
from src.digest.render import DigestData, Topic
from src.web import app as web_app
from src.web import auth

OWNER = 132036441
BOT_ID = 8123456789
DAY = date(2026, 9, 24)
TZ = "Europe/Moscow"


# ============================================================ сообщения ботов

def test_bot_id_comes_from_the_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "BOT_TOKEN", f"{BOT_ID}:AAE-secret")
    assert cfg.bot_id == BOT_ID
    monkeypatch.setattr(cfg, "BOT_TOKEN", "")
    assert cfg.bot_id is None


def _msg(chat_id: int, tg_msg_id: int, user_id: int, text: str) -> Message:
    return Message(chat_id=chat_id, tg_msg_id=tg_msg_id, tg_user_id=user_id,
                   author_name="кто-то", text=text,
                   date=datetime(2026, 9, 24, 12, tg_msg_id, tzinfo=ZoneInfo(TZ)))


async def test_bot_messages_stay_out_of_digest_and_ratings(
    db: None, monkeypatch: pytest.MonkeyPatch,
):
    """«Готово!» нашего бота и сообщения чужих ботов — не обсуждение людей."""
    monkeypatch.setattr(repo.cfg, "BOT_TOKEN", f"{BOT_ID}:AAE-secret")
    chat = await repo.get_or_create_chat(-100111, "Группа")
    await repo.upsert_author(555, "Вася")
    await repo.upsert_author(777, "Другой бот", is_bot=True)
    for n, (user, text) in enumerate([(555, "вопрос людям"), (BOT_ID, "Готово!"),
                                      (777, "реклама бота"), (555, "ещё мысль")], start=1):
        await repo.upsert_message(_msg(chat.id, n, user, text))

    everything = await repo.get_messages_by_day(chat.id, DAY, tz=TZ)
    people = await repo.get_messages_by_day(chat.id, DAY, tz=TZ, exclude_bots=True)

    assert len(everything) == 4
    assert [m.text for m in people] == ["вопрос людям", "ещё мысль"]


async def test_bot_mark_is_not_lost_on_a_later_update(db: None):
    await repo.upsert_author(777, "Бот", is_bot=True)
    again = await repo.upsert_author(777, "Бот переименован")   # имя без признака
    assert again.is_bot is True and again.name == "Бот переименован"


async def test_digest_reads_messages_without_bots(monkeypatch: pytest.MonkeyPatch):
    from src.digest import pipeline

    seen: dict[str, Any] = {}

    async def get_messages_by_day(chat_id: int, day: date, **kw: Any) -> list[Message]:
        seen.update(kw)
        raise StopAsyncIteration   # дальше не идём — важен только запрос

    monkeypatch.setattr(pipeline.repo, "get_messages_by_day", get_messages_by_day)
    with pytest.raises(StopAsyncIteration):
        await pipeline.build_digest(Chat(id=1, tg_id=-100111), DAY)
    assert seen.get("exclude_bots") is True


async def test_collector_marks_bots(monkeypatch: pytest.MonkeyPatch):
    from src.collector.handlers import AuthorCache

    saved: list[tuple[Any, ...]] = []

    async def upsert_author(user_id: int, name: str | None, *, is_bot: bool = False) -> None:
        saved.append((user_id, name, is_bot))

    monkeypatch.setattr("src.collector.handlers.repo.upsert_author", upsert_author)

    async def sender() -> Any:
        return SimpleNamespace(first_name="Помощник", last_name=None, username="helper_bot",
                               bot=True)

    message = SimpleNamespace(sender_id=777, get_sender=sender)
    await AuthorCache().resolve(message)
    assert saved == [(777, "Помощник", True)]


# ================================================================ темы форума

def test_topics_skip_deleted_ones():
    result = SimpleNamespace(topics=[
        SimpleNamespace(id=1, title="General", closed=True),
        SimpleNamespace(id=42, title="Дайджесты", closed=False),
        SimpleNamespace(id=43),   # ForumTopicDeleted — без названия
    ])
    assert forum.topic_rows(result) == [
        {"id": 1, "title": "General", "closed": True},
        {"id": 42, "title": "Дайджесты", "closed": False},
    ]


async def test_group_without_topics_is_remembered_as_such(monkeypatch: pytest.MonkeyPatch):
    stored: dict[str, Any] = {}

    async def set_state(key: str, value: Any) -> None:
        stored[key] = value

    class Client:
        async def get_entity(self, tg_id: int) -> Any:
            return SimpleNamespace(forum=False)

        async def __call__(self, request: Any) -> Any:
            raise AssertionError("в группе без тем темы не запрашиваем")

    monkeypatch.setattr(forum.repo, "set_state", set_state)
    assert await forum.sync_topics(Client(), Chat(id=3, tg_id=-100111)) == []
    assert stored["topics:3"]["topics"] == []


async def test_forum_topics_are_stored(monkeypatch: pytest.MonkeyPatch):
    stored: dict[str, Any] = {}

    async def set_state(key: str, value: Any) -> None:
        stored[key] = value

    class Client:
        async def get_entity(self, tg_id: int) -> Any:
            return SimpleNamespace(forum=True)

        async def __call__(self, request: Any) -> Any:
            return SimpleNamespace(topics=[SimpleNamespace(id=42, title="Итоги", closed=False)])

    monkeypatch.setattr(forum.repo, "set_state", set_state)
    await forum.sync_topics(Client(), Chat(id=3, tg_id=-100111))
    assert stored["topics:3"]["topics"] == [{"id": 42, "title": "Итоги", "closed": False}]


# ================================================= публикация в выбранную тему

@pytest.mark.parametrize(("setting", "thread"), [
    (None, None), (1, None),      # «Общая» — без message_thread_id
    (42, 42), ("42", 42), ("мусор", None),
])
def test_publish_topic(setting: Any, thread: int | None):
    chat = Chat(id=1, tg_id=-100111, settings={"publish_topic": setting} if setting else {})
    assert publish.publish_topic(chat) == thread


def test_closed_topic_is_explained_in_plain_words():
    text = publish.explain_telegram_error(
        RuntimeError("Telegram server says - Bad Request: TOPIC_CLOSED")
    )
    assert "тема" in text and "закрыта" in text and "«Группах»" in text
    assert publish.explain_telegram_error(RuntimeError("что-то новое")) == "что-то новое"


def _payload() -> dict[str, Any]:
    return DigestData(
        chat_tg_id=-100111, day=DAY, chat_title="Группа", highlights=["Главное"],
        topics=[Topic(thread_id=11, title="Цены", kind="insight", msg_count=20,
                      summary="Пересказ", key_msg_ids=[5])],
        msg_count=100, participants=9,
    ).to_dict()


class SendingBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error = error

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> Any:
        if self.error:
            raise self.error
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=900 + len(self.sent))


@pytest.fixture
def publish_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Подставные группа и дайджест; бот — подставной, в живой Telegram ничего не уходит."""
    world: dict[str, Any] = {
        "chat": Chat(id=1, tg_id=-100111, title="Группа", publish="manual", portal="off",
                     settings={"publish_topic": 42}),
        "released": 0,
    }
    digest = Digest(id=5, chat_id=1, day=DAY, summary_md="", payload=_payload(), msg_count=100)

    async def get_digest_by_id(digest_id: int) -> Digest:
        return digest

    async def get_chat_by_id(chat_id: int) -> Chat:
        return world["chat"]

    async def claim(digest_id: int) -> bool:
        return True

    async def finish(digest_id: int, ids: list[int]) -> None:
        world["finished"] = ids

    async def release(digest_id: int) -> None:
        world["released"] += 1

    for name, fn in {"get_digest_by_id": get_digest_by_id, "get_chat_by_id": get_chat_by_id,
                     "claim_digest_publication": claim, "finish_digest_publication": finish,
                     "release_digest_publication": release}.items():
        monkeypatch.setattr(publish.repo, name, fn)
    return world


async def test_digest_goes_to_the_chosen_topic(publish_world: dict[str, Any]):
    bot = SendingBot()
    result = await publish.publish_digest(bot, 5)
    assert result.ok
    assert {m["message_thread_id"] for m in bot.sent} == {42}


async def test_closed_topic_failure_is_readable_and_retryable(publish_world: dict[str, Any]):
    bot = SendingBot(RuntimeError("Telegram server says - Bad Request: TOPIC_CLOSED"))
    result = await publish.publish_digest(bot, 5)
    assert not result.ok
    assert "закрыта" in result.text and "TOPIC_CLOSED" not in result.text
    assert publish_world["released"] == 1, "отметку сняли — после исправления можно повторить"


# ============================================================ выбор темы в админке

@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for target in {id(cfg): cfg, id(auth.cfg): auth.cfg, id(web_app.cfg): web_app.cfg}.values():
        monkeypatch.setattr(target, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
        monkeypatch.setattr(target, "OWNER_ID", OWNER)
    world: dict[str, Any] = {
        "chat": Chat(id=1, tg_id=-100111, title="Группа", publish="manual", settings={}),
        "saved": [],
        "states": {"topics:1": {"topics": [
            {"id": 1, "title": "General", "closed": True},
            {"id": 42, "title": "Итоги <b>дня</b>", "closed": False},
        ]}},
    }

    async def get_chat_by_tg_id(tg_id: int) -> Chat:
        return world["chat"]

    async def list_chats(**kw: Any) -> list[Chat]:
        return [world["chat"]]

    async def get_states(prefix: str = "") -> dict[str, Any]:
        return {k: v for k, v in world["states"].items() if k.startswith(prefix)}

    async def update_chat_settings(chat_id: int, patch: dict[str, Any]) -> None:
        world["saved"].append(patch)
        world["chat"].settings = {**(world["chat"].settings or {}), **patch}

    for name, fn in {"get_chat_by_tg_id": get_chat_by_tg_id, "list_chats": list_chats,
                     "get_states": get_states,
                     "update_chat_settings": update_chat_settings}.items():
        monkeypatch.setattr(web_app.repo, name, fn)
    monkeypatch.setattr("src.bot.middlewares.settings_cache.forget", lambda *a: None)
    return world


def owner_client() -> TestClient:
    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    client.cookies.set(auth.COOKIE_NAME, auth.issue_session(OWNER))
    return client


def test_groups_page_offers_forum_topics(admin: dict[str, Any]):
    page = owner_client().get("/groups").text
    assert "В какую тему публиковать" in page
    assert '<option value="42"' in page and "Итоги &lt;b&gt;дня&lt;/b&gt;" in page
    # по умолчанию — «Общая», и она закрыта: владелец видит, почему публикация не пройдёт
    assert "General — закрыта" in page and "Telegram не даст боту" in page


def test_owner_picks_a_topic(admin: dict[str, Any]):
    client = owner_client()
    csrf = auth.read_session(client.cookies.get(auth.COOKIE_NAME))["csrf"]
    row = client.post("/groups/-100111", data={"field": "publish_topic", "value": "42",
                                               "csrf_token": csrf})
    assert row.status_code == 200 and admin["saved"] == [{"publish_topic": 42}]
    assert '<option value="42" selected' in row.text and "Telegram не даст" not in row.text

    bad = client.post("/groups/-100111", data={"field": "publish_topic", "value": "999",
                                               "csrf_token": csrf})
    assert bad.status_code == 400 and len(admin["saved"]) == 1


def test_no_topic_picker_in_a_group_without_topics(admin: dict[str, Any]):
    admin["states"] = {"topics:1": {"topics": []}}
    assert "В какую тему публиковать" not in owner_client().get("/groups").text


async def test_issue_number_counts_digests_up_to_the_day(db: None):
    chat = await repo.get_or_create_chat(-100111, "Группа")
    for day in (date(2026, 9, 20), date(2026, 9, 22), date(2026, 9, 24)):
        await repo.save_digest(chat.id, day, "текст")
    assert await repo.digest_issue_number(chat.id, date(2026, 9, 22)) == 2
    assert await repo.digest_issue_number(chat.id, date(2026, 9, 24)) == 3
