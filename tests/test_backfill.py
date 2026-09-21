"""Тесты бэкфилла (TZ шаг 4). Живой Telegram не нужен."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from src.collector import backfill as bf
from src.collector.service import Collector
from src.db.models import Chat
from tests.test_collector import fake_message

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _chat() -> Chat:
    return Chat(id=3, tg_id=-1002354231333, title="Группа", collect=True)


def history(count: int, *, newest: datetime = NOW, step_min: int = 10) -> list[Any]:
    """Сообщения от свежих к старым, как их отдаёт Telegram."""
    messages = []
    for n in range(count):
        msg = fake_message(id=count - n, date=newest - timedelta(minutes=step_min * n))

        async def get_sender() -> Any:
            return SimpleNamespace(first_name="Вася", last_name=None, username=None)

        msg.get_sender = get_sender
        messages.append(msg)
    return messages


class FakeClient:
    """Отдаёт историю страницами так же, как Telethon.get_messages."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages  # от свежих к старым
        self.calls: list[dict[str, Any]] = []

    async def get_messages(self, chat_tg_id: int, *, limit: int, offset_id: int) -> list[Any]:
        self.calls.append({"chat": chat_tg_id, "limit": limit, "offset_id": offset_id})
        pool = self.messages
        if offset_id:
            pool = [m for m in pool if m.id < offset_id]
        return pool[:limit]


@pytest.fixture
def no_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Подменяет state в памяти: тесты не ходят в БД."""
    store: dict[str, Any] = {}

    async def get_state(key: str, default: Any = None) -> Any:
        return store.get(key, default)

    async def set_state(key: str, value: Any) -> None:
        store[key] = value

    monkeypatch.setattr(bf.repo, "get_state", get_state)
    monkeypatch.setattr(bf.repo, "set_state", set_state)
    return store


@pytest.fixture
def no_pause(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)

    monkeypatch.setattr(bf.asyncio, "sleep", fake_sleep)
    return slept


# ------------------------------------------------------------------ порции

async def test_reads_in_batches_with_pause(no_state, no_pause, capsys):
    """ТЗ: порции по 200 с паузой 1.5 секунды."""
    client = FakeClient(history(450))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    result = await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    assert result.saved == 450
    assert result.batches == 3, "450 сообщений = 200 + 200 + 50"
    assert [c["limit"] for c in client.calls] == [200, 200, 200]
    assert no_pause == [1.5, 1.5], "пауза между порциями, но не после последней"


async def test_offset_moves_to_oldest_of_previous_batch(no_state, no_pause):
    """Следующая порция запрашивается от самого старого сообщения предыдущей."""
    client = FakeClient(history(300))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    assert client.calls[0]["offset_id"] == 0, "первая порция — от самого свежего"
    assert client.calls[1]["offset_id"] == 101, "id самого старого из первой сотни"


# ------------------------------------------------------------------- глубина

async def test_stops_at_cutoff_date(no_state, no_pause):
    """Грузим только последние N дней, а не всю историю."""
    # 100 сообщений с шагом в час; в сутки укладываются NOW .. NOW-24ч
    client = FakeClient(history(100, step_min=60))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    result = await bf.backfill_chat(collector, _chat(), days=1, now=NOW)

    assert result.saved == 25, "включая сообщение ровно на границе суток"
    assert result.done is True
    assert result.oldest_date == NOW - timedelta(hours=24)
    assert result.scanned == 26, "просмотрели на одно больше — им и остановились"


async def test_short_batch_means_history_is_over(no_state, no_pause):
    client = FakeClient(history(30))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    result = await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    assert result.saved == 30
    assert result.done is True
    assert len(client.calls) == 1, "второй запрос не нужен — Telegram отдал меньше лимита"


async def test_empty_history(no_state, no_pause):
    collector = Collector(FakeClient([]), dry_run=False)
    monkeypatched_save(collector)

    result = await bf.backfill_chat(collector, _chat(), days=30, now=NOW)

    assert (result.saved, result.done) == (0, True)


# --------------------------------------------------------------- возобновление

async def test_resumes_from_saved_point(no_state, no_pause):
    """Прерванный бэкфилл продолжается, а не начинается заново."""
    no_state[bf.state_key(-1002354231333)] = {"offset_id": 150, "done": False}
    client = FakeClient(history(300))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    assert client.calls[0]["offset_id"] == 150, "продолжили с сохранённой точки"


async def test_saves_progress_after_every_batch(no_state, no_pause):
    """Точка остановки пишется после каждой порции, а не в конце."""
    client = FakeClient(history(450))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    saved = no_state[bf.state_key(-1002354231333)]
    assert saved["done"] is True
    assert saved["offset_id"] == 1


async def test_finished_chat_is_not_reloaded(no_state, no_pause):
    no_state[bf.state_key(-1002354231333)] = {"offset_id": 1, "done": True}
    client = FakeClient(history(100))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    result = await bf.backfill_chat(collector, _chat(), days=30, now=NOW)

    assert result.saved == 0
    assert client.calls == [], "к Telegram не ходим впустую"


async def test_restart_ignores_saved_state(no_state, no_pause):
    no_state[bf.state_key(-1002354231333)] = {"offset_id": 1, "done": True}
    client = FakeClient(history(50))
    collector = Collector(client, dry_run=False)
    monkeypatched_save(collector)

    result = await bf.backfill_chat(collector, _chat(), days=3650, restart=True, now=NOW)

    assert result.saved == 50
    assert client.calls[0]["offset_id"] == 0


# ------------------------------------------------------------------- прочее

async def test_service_messages_are_skipped(no_state, no_pause):
    """«Вася добавил Петю» — это шум для дайджеста и поиска."""
    messages = history(5)
    messages[2].action = object()
    collector = Collector(FakeClient(messages), dry_run=False)
    monkeypatched_save(collector)

    result = await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    assert result.saved == 4
    assert result.scanned == 5


async def test_one_broken_message_does_not_stop_backfill(no_state, no_pause):
    messages = history(5)
    collector = Collector(FakeClient(messages), dry_run=False)
    saved_ids: list[int] = []

    async def save(message: Any, chat: Chat) -> None:
        if message.id == 3:
            raise ValueError("битое сообщение")
        saved_ids.append(message.id)

    collector.save = save  # type: ignore[method-assign]

    result = await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    assert result.saved == 4
    assert 3 not in saved_ids


async def test_dry_run_does_not_touch_state(no_state, no_pause):
    collector = Collector(FakeClient(history(10)), dry_run=True)
    monkeypatched_save(collector)

    await bf.backfill_chat(collector, _chat(), days=3650, now=NOW)

    assert no_state == {}, "в режиме проверки ничего не запоминаем"


def test_result_describe_is_readable():
    result = bf.BackfillResult(
        chat_tg_id=-100, saved=120, scanned=130, batches=2, done=True, oldest_date=NOW
    )
    text = result.describe()
    assert "120" in text and "130" in text and "история пройдена" in text


def monkeypatched_save(collector: Collector) -> None:
    """Сохранение в БД подменяем счётчиком: здесь проверяется логика обхода."""
    async def save(message: Any, chat: Chat) -> None:
        collector.saved += 1

    collector.save = save  # type: ignore[method-assign]
