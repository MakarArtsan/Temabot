"""Тесты коллектора (TZ шаг 3). Живой Telegram не нужен — объекты подделаны."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from src.collector import client as client_mod
from src.collector.handlers import (
    AuthorCache,
    author_display_name,
    count_reactions,
    detect_media_type,
    extract_thread_refs,
    normalize_message,
)
from src.collector.service import Collector
from src.db.models import Chat
from telethon.errors import FloodWaitError

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def fake_message(**kw: Any) -> SimpleNamespace:
    """Telethon-сообщение с нужными атрибутами; остальное — None, как у него."""
    defaults: dict[str, Any] = dict(
        id=1,
        message="привет",
        sender_id=1001,
        date=NOW,
        edit_date=None,
        reply_to=None,
        reply_to_msg_id=None,
        reactions=None,
        fwd_from=None,
        grouped_id=None,
        views=None,
        post_author=None,
        pinned=False,
        voice=None,
        video_note=None,
        audio=None,
        photo=None,
        sticker=None,
        gif=None,
        video=None,
        document=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# -------------------------------------------------------------------- медиа

@pytest.mark.parametrize(
    ("attrs", "expected"),
    [
        ({}, None),
        ({"voice": object()}, "voice"),
        ({"video_note": object()}, "voice"),
        ({"audio": object()}, "audio"),
        ({"photo": object()}, "photo"),
        ({"video": object()}, "video"),
        ({"document": object()}, "doc"),
        ({"sticker": object()}, "sticker"),
    ],
)
def test_detect_media_type(attrs: dict[str, Any], expected: str | None):
    assert detect_media_type(fake_message(**attrs)) == expected


def test_voice_wins_over_document():
    """Голосовое — это тоже документ; распознать надо голосовое, его расшифровывают."""
    assert detect_media_type(fake_message(voice=object(), document=object())) == "voice"


# ------------------------------------------------------------------ реакции

def test_count_reactions_sums_all_emoji():
    reactions = SimpleNamespace(
        results=[SimpleNamespace(count=3), SimpleNamespace(count=2), SimpleNamespace(count=0)]
    )
    assert count_reactions(fake_message(reactions=reactions)) == 5


def test_count_reactions_without_reactions():
    assert count_reactions(fake_message()) == 0
    assert count_reactions(fake_message(reactions=SimpleNamespace(results=None))) == 0


# ------------------------------------------------------- reply и форумные темы

def test_plain_reply():
    msg = fake_message(reply_to=SimpleNamespace(reply_to_msg_id=5, forum_topic=False))
    assert extract_thread_refs(msg) == (5, None)


def test_message_without_reply():
    assert extract_thread_refs(fake_message()) == (None, None)


def test_forum_message_posted_in_topic_is_not_a_reply():
    """Сообщение в теме без ответа кому-либо не должно склеивать всю тему в цепочку."""
    msg = fake_message(
        reply_to=SimpleNamespace(reply_to_msg_id=100, forum_topic=True, reply_to_top_id=None)
    )
    assert extract_thread_refs(msg) == (None, 100)


def test_forum_reply_keeps_both_refs():
    msg = fake_message(
        reply_to=SimpleNamespace(reply_to_msg_id=142, forum_topic=True, reply_to_top_id=100)
    )
    assert extract_thread_refs(msg) == (142, 100)


# -------------------------------------------------------------------- автор

@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        (SimpleNamespace(first_name="Вася", last_name="Пупкин", username="vasya"), "Вася Пупкин"),
        (SimpleNamespace(first_name="Вася", last_name=None, username="vasya"), "Вася"),
        (SimpleNamespace(first_name=None, last_name=None, username="vasya"), "@vasya"),
        (SimpleNamespace(title="Канал"), "Канал"),
        (None, None),
    ],
)
def test_author_display_name(sender: Any, expected: str | None):
    assert author_display_name(sender) == expected


# ---------------------------------------------------------------- нормализация

def test_normalize_message_maps_fields():
    msg = fake_message(
        id=77,
        message="Seedance режет ролики",
        sender_id=1001,
        reply_to=SimpleNamespace(reply_to_msg_id=70, forum_topic=False),
        reactions=SimpleNamespace(results=[SimpleNamespace(count=4)]),
        edit_date=NOW,
    )
    row = normalize_message(msg, chat_id=3, author_name="Вася")

    assert row.chat_id == 3
    assert row.tg_msg_id == 77
    assert row.tg_user_id == 1001
    assert row.author_name == "Вася"
    assert row.text == "Seedance режет ролики"
    assert row.reply_to == 70
    assert row.reactions == 4
    assert row.edited_at == NOW
    assert row.source == "collector"


def test_normalize_empty_caption_becomes_none():
    """Пустая строка в text мешала бы coalesce в upsert перетирать осмысленный текст."""
    assert normalize_message(fake_message(message=""), chat_id=1).text is None


def test_normalize_keeps_raw_compact():
    """Полный raw раздул бы базу — храним только флаги."""
    row = normalize_message(fake_message(fwd_from=object(), views=10, pinned=True), chat_id=1)
    assert row.raw == {"forwarded": True, "views": 10, "pinned": True}
    assert normalize_message(fake_message(), chat_id=1).raw == {}


# --------------------------------------------------------------- кэш авторов

def test_author_cache_ttl():
    cache = AuthorCache(ttl_sec=24 * 3600)
    cache.put(1001, "Вася", now=NOW)

    assert cache.is_fresh(1001, now=NOW + timedelta(hours=23)) is True
    assert cache.is_fresh(1001, now=NOW + timedelta(hours=25)) is False
    assert cache.is_fresh(999, now=NOW) is False
    assert cache.get(1001) == "Вася"


async def test_author_cache_survives_failed_lookup():
    """Если Telegram не отдал автора, сообщение всё равно должно сохраниться."""
    cache = AuthorCache()

    async def boom() -> Any:
        raise ConnectionError("нет сети")

    msg = fake_message()
    msg.get_sender = boom
    assert await cache.resolve(msg, persist=False) is None


# ------------------------------------------------------------- FloodWaitError

async def test_with_flood_retry_waits_and_retries(monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    # FloodWaitError берёт seconds из запроса; подставляем вручную
    async def action_with_seconds() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            err = FloodWaitError(request=None)
            err.seconds = 30
            raise err
        return "ok"

    assert await client_mod.with_flood_retry(action_with_seconds) == "ok"
    assert calls["n"] == 2
    assert slept and 30 <= slept[0] <= 32, "ждём ровно столько, сколько просит Telegram"


async def test_with_flood_retry_gives_up_on_absurd_wait(monkeypatch: pytest.MonkeyPatch):
    """Ожидание больше часа — это не временный лимит, а повод остановиться."""
    monkeypatch.setattr(client_mod.asyncio, "sleep", lambda *_: asyncio.sleep(0))

    async def action() -> str:
        err = FloodWaitError(request=None)
        err.seconds = 90_000
        raise err

    with pytest.raises(FloodWaitError):
        await client_mod.with_flood_retry(action)


async def test_with_flood_retry_backs_off_on_network_errors(monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)

    async def action() -> str:
        raise ConnectionError("сеть отвалилась")

    with pytest.raises(ConnectionError):
        await client_mod.with_flood_retry(action, attempts=4, base_delay=2.0)

    assert slept == [2.0, 4.0, 8.0, 16.0], "экспоненциальная пауза по ТЗ"


# ---------------------------------------------------------------------- прокси

def test_parse_proxy():
    assert client_mod.parse_proxy("") is None
    assert client_mod.parse_proxy("socks5://user:pass@10.0.0.1:1080") == {
        "proxy_type": "socks5",
        "addr": "10.0.0.1",
        "port": 1080,
        "rdns": True,
        "username": "user",
        "password": "pass",
    }
    assert client_mod.parse_proxy("socks5://10.0.0.1:1080") == {
        "proxy_type": "socks5",
        "addr": "10.0.0.1",
        "port": 1080,
        "rdns": True,
    }


def test_parse_proxy_rejects_incomplete_url():
    with pytest.raises(ValueError, match="PROXY_URL"):
        client_mod.parse_proxy("socks5://10.0.0.1")


# ------------------------------------------------------------------- dry-run

class FakeClient:
    """Клиент, отдающий заранее заданные сообщения в iter_messages."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.iter_calls: list[dict[str, Any]] = []

    def iter_messages(self, chat_tg_id: int, **kw: Any):
        self.iter_calls.append({"chat": chat_tg_id, **kw})

        async def gen():
            for m in self.messages:
                if kw.get("min_id") and m.id <= kw["min_id"]:
                    continue
                yield m

        return gen()


def _chat() -> Chat:
    return Chat(id=3, tg_id=-1001234567890, title="Группа", collect=True)


async def test_dry_run_prints_and_does_not_touch_db(capsys, monkeypatch: pytest.MonkeyPatch):
    from src.db import repo

    async def explode(*a: Any, **kw: Any) -> None:
        raise AssertionError("dry-run не должен писать в БД")

    monkeypatch.setattr(repo, "upsert_message", explode)
    monkeypatch.setattr(repo, "upsert_author", explode)

    collector = Collector(FakeClient([]), dry_run=True)
    msg = fake_message(id=42, message="тестовое сообщение")
    msg.get_sender = _sender("Вася")

    await collector.save(msg, _chat())

    out = capsys.readouterr().out
    assert "[dry-run]" in out and "#42" in out and "тестовое сообщение" in out
    assert collector.saved == 1


def _sender(name: str):
    async def get_sender() -> Any:
        return SimpleNamespace(first_name=name, last_name=None, username=None)

    return get_sender


# -------------------------------------------------------------------- докачка

async def test_catch_up_reads_only_messages_after_last_known(monkeypatch: pytest.MonkeyPatch):
    """Рестарт не должен терять сообщения и не должен перечитывать всё заново."""
    from src.db import repo

    async def last_id(chat_id: int) -> int:
        return 10

    monkeypatch.setattr(repo, "get_last_tg_msg_id", last_id)

    messages = [fake_message(id=i) for i in (9, 10, 11, 12)]
    for m in messages:
        m.get_sender = _sender("Вася")
    client = FakeClient(messages)
    collector = Collector(client, dry_run=True)

    fetched = await collector.catch_up(_chat())

    assert fetched == 2, "берём только 11 и 12"
    assert client.iter_calls[0]["min_id"] == 10
    assert client.iter_calls[0]["reverse"] is True, "порядок по возрастанию, иначе треды сломаются"


async def test_catch_up_skips_empty_chat(monkeypatch: pytest.MonkeyPatch):
    """Пустой чат — это работа для бэкфилла, а не для докачки."""
    from src.db import repo

    async def last_id(chat_id: int) -> None:
        return None

    monkeypatch.setattr(repo, "get_last_tg_msg_id", last_id)
    client = FakeClient([fake_message(id=1)])

    assert await Collector(client, dry_run=True).catch_up(_chat()) == 0
    assert client.iter_calls == []


async def test_save_batch_survives_one_bad_message(monkeypatch: pytest.MonkeyPatch):
    """Одно битое сообщение не должно ронять всю порцию докачки."""
    collector = Collector(FakeClient([]), dry_run=True)
    good = fake_message(id=1)
    good.get_sender = _sender("Вася")
    bad = fake_message(id=2)
    bad.date = None
    bad.get_sender = _sender("Петя")

    original = collector.save

    async def save(message: Any, chat: Chat) -> None:
        if message.id == 2:
            raise ValueError("битое сообщение")
        await original(message, chat)

    monkeypatch.setattr(collector, "save", save)
    assert await collector._save_batch([good, bad], _chat()) == 1


# ------------------------------------------------------- маршрутизация событий

async def test_events_from_foreign_chats_are_ignored():
    """Коллектор пишет только те группы, что включены (TZ §4.8)."""
    collector = Collector(FakeClient([]), dry_run=False)
    collector._chats_by_tg_id = {-1001234567890: _chat()}

    event = SimpleNamespace(chat_id=-100999, message=fake_message(), id=1)
    await collector.on_new_message(event)  # не должно упасть и не должно писать

    assert collector.saved == 0
    assert collector.chat_for(-100999) is None
    assert collector.chat_for(-1001234567890) is not None
