"""Тесты бота и расписания (TZ шаг 7). Telegram не вызывается."""
from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from src.bot.main import build_dispatcher
from src.bot.middlewares import OwnerOnly, RateLimit
from src.digest import scheduler as sched
from src.digest.render import (
    DigestData,
    Topic,
    esc_html,
    esc_md,
    render,
    render_html,
    split_message,
)

OWNER = 132036441
STRANGER = 999999


def user(user_id: int) -> Any:
    return SimpleNamespace(id=user_id, full_name="кто-то")


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kw: Any) -> None:
        self.answers.append(text)


# ------------------------------------------------------------ доступ к боту

async def test_owner_passes():
    middleware = OwnerOnly(OWNER)
    called = False

    async def handler(event: Any, data: dict[str, Any]) -> str:
        nonlocal called
        called = True
        return "ok"

    result = await middleware(handler, FakeMessage(), {"event_from_user": user(OWNER)})
    assert called and result == "ok"


async def test_stranger_is_ignored_silently():
    """Вежливый отказ подтвердил бы, что бот что-то знает о группе (TZ §9)."""
    middleware = OwnerOnly(OWNER)
    message = FakeMessage()
    called = False

    async def handler(event: Any, data: dict[str, Any]) -> str:
        nonlocal called
        called = True
        return "ok"

    result = await middleware(handler, message, {"event_from_user": user(STRANGER)})

    assert result is None
    assert called is False
    assert message.answers == [], "чужому не отвечаем вообще"


async def test_event_without_user_is_ignored():
    middleware = OwnerOnly(OWNER)

    async def handler(event: Any, data: dict[str, Any]) -> str:
        raise AssertionError("не должно вызваться")

    assert await middleware(handler, FakeMessage(), {}) is None


async def test_callback_from_stranger_gets_alert():
    """У кнопки молчание выглядит как поломка, поэтому здесь отвечаем."""
    from aiogram.types import CallbackQuery

    answered: list[dict[str, Any]] = []

    class FakeCallback(CallbackQuery):
        async def answer(self, text: str | None = None, **kw: Any) -> None:  # type: ignore[override]
            answered.append({"text": text, **kw})

    callback = FakeCallback.model_construct(id="1", from_user=user(STRANGER), chat_instance="x")

    async def handler(event: Any, data: dict[str, Any]) -> str:
        raise AssertionError("не должно вызваться")

    await OwnerOnly(OWNER)(handler, callback, {"event_from_user": user(STRANGER)})
    assert answered and "владельцу" in answered[0]["text"]


# ------------------------------------------------------------- ограничитель

def test_rate_limit_allows_up_to_the_cap():
    limiter = RateLimit(limit=3, window_sec=60)
    assert [limiter.allow(OWNER, now=0.0) for _ in range(4)] == [True, True, True, False]


def test_rate_limit_window_moves():
    limiter = RateLimit(limit=2, window_sec=60)
    assert limiter.allow(OWNER, now=0.0) is True
    assert limiter.allow(OWNER, now=1.0) is True
    assert limiter.allow(OWNER, now=2.0) is False
    assert limiter.allow(OWNER, now=61.5) is True, "старые попытки вышли из окна"


def test_rate_limit_is_per_user():
    limiter = RateLimit(limit=1, window_sec=60)
    assert limiter.allow(OWNER, now=0.0) is True
    assert limiter.allow(STRANGER, now=0.0) is True


# ----------------------------------------------------- длинные сообщения

def test_short_text_is_not_split():
    assert split_message("короткий текст") == ["короткий текст"]


def test_split_happens_on_paragraph_borders():
    blocks = [f"Абзац номер {n} " + "x" * 400 for n in range(20)]
    parts = split_message("\n\n".join(blocks), limit=1000)

    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    assert all(not p.startswith("\n") for p in parts)
    # ни один абзац не разорван посередине
    assert sum(p.count("Абзац номер") for p in parts) == 20


def test_single_huge_paragraph_is_cut():
    parts = split_message("\n".join(f"строка {n}" for n in range(500)), limit=200)
    assert all(len(p) <= 200 for p in parts)
    assert "".join(parts).count("строка") == 500


# --------------------------------------------------------------- экранирование

def test_html_escaping_of_model_output():
    """Модель может вернуть что угодно, Telegram не должен падать на разборе."""
    assert esc_html("<b>хак</b> & <script>") == "&lt;b&gt;хак&lt;/b&gt; &amp; &lt;script&gt;"


def test_markdown_escaping():
    assert esc_md("цена *100* [руб]") == r"цена \*100\* \[руб\]"


def test_render_html_escapes_topic_titles():
    data = DigestData(
        chat_tg_id=-1002354231333,
        day=date(2026, 9, 20),
        topics=[Topic(thread_id=1, title="Цена <b>выросла</b> & упала", msg_count=3)],
        msg_count=10,
    )
    text = render_html(data)

    assert "&lt;b&gt;выросла&lt;/b&gt;" in text
    assert "<b>Цена &lt;b&gt;выросла" in text, "заголовок остаётся жирным"
    assert "&amp;" in text


def test_both_formats_carry_the_same_facts():
    data = DigestData(
        chat_tg_id=-1002354231333,
        day=date(2026, 9, 20),
        chat_title="Группа",
        highlights=["главное"],
        topics=[Topic(thread_id=5, title="Тема", decision="вывод", msg_count=4)],
        msg_count=10,
        participants=3,
    )
    md, html = render(data), render_html(data)

    for text in (md, html):
        assert "Тема" in text and "вывод" in text and "главное" in text
        assert "https://t.me/c/2354231333/5" in text
    assert "<b>" in html and "<b>" not in md


# ------------------------------------------------- сохранение и восстановление

def test_digest_survives_a_round_trip_through_the_database():
    """`/digest <дата>` должен показать ровно то, что ушло в первый раз."""
    topic = Topic(
        thread_id=5, title="Seedance", decision="ужимать до 1920",
        links=["https://example.com"], key_msg_ids=[7], msg_count=4,
        participants=["Вася", "Петя"],
    )
    data = DigestData(
        chat_tg_id=-1002354231333,
        day=date(2026, 9, 20),
        chat_title="Группа",
        highlights=["главное за день"],
        topics=[topic],
        unanswered=[("а что с 4K?", 9)],
        links=["https://example.com"],
        msg_count=42,
        participants=5,
        noise_count=3,
        busiest_thread=topic,   # так его заполняет pipeline
    )
    restored = DigestData.from_dict(data.to_dict())

    assert render_html(restored) == render_html(data)
    assert restored.topics[0].key_msg_ids == [7]
    assert restored.unanswered == [("а что с 4K?", 9)]


def test_old_records_with_plain_topic_list_still_load():
    """В первых записях в topics лежал просто список тем."""
    from src.db.models import Digest

    digest = Digest.from_row({
        "id": 1, "chat_id": 1, "day": date(2026, 9, 20), "summary_md": "текст",
        "topics": [{"title": "Тема"}], "msg_count": 5, "tokens_used": 0,
        "created_at": None,
    })
    assert digest.topics == [{"title": "Тема"}]
    assert digest.payload == {"topics": [{"title": "Тема"}]}


# ------------------------------------------------------------------ расписание

def test_scheduler_uses_kamchatka_and_half_past_eleven():
    scheduler = sched.build_scheduler(bot=object())
    job = scheduler.get_job("daily_digest")

    assert str(job.trigger.timezone) == "Asia/Kamchatka"
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "23" and fields["minute"] == "30"


def test_scheduler_does_not_double_send_after_downtime():
    """Процесс мог лежать: пропущенные запуски должны схлопнуться в один."""
    job = sched.build_scheduler(bot=object()).get_job("daily_digest")
    assert job.coalesce is True
    assert job.misfire_grace_time >= 3600


async def test_one_broken_group_does_not_cancel_the_rest(monkeypatch: pytest.MonkeyPatch):
    from src.db.models import Chat

    chats = [
        Chat(id=1, tg_id=-100111, title="Сломанная", digest=True),
        Chat(id=2, tg_id=-100222, title="Рабочая", digest=True),
    ]

    async def list_chats(**kw: Any) -> list[Chat]:
        return chats

    async def run_for_chat(chat: Chat, day: date, **kw: Any) -> Any:
        if chat.id == 1:
            raise RuntimeError("модель не ответила")
        return SimpleNamespace(html="<b>Дайджест</b>")

    async def set_state(key: str, value: Any) -> None:
        return None

    sent: list[str] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
            sent.append(text)

    monkeypatch.setattr(sched.repo, "list_chats", list_chats)
    monkeypatch.setattr(sched.repo, "set_state", set_state)
    monkeypatch.setattr(sched.digest_pipeline, "run_for_chat", run_for_chat)

    count = await sched.send_daily_digests(FakeBot(), day=date(2026, 9, 20))

    assert count == 1
    assert sent == ["<b>Дайджест</b>"]


async def test_run_records_last_run_state(monkeypatch: pytest.MonkeyPatch):
    saved: dict[str, Any] = {}

    async def list_chats(**kw: Any) -> list[Any]:
        return []

    async def set_state(key: str, value: Any) -> None:
        saved[key] = value

    monkeypatch.setattr(sched.repo, "list_chats", list_chats)
    monkeypatch.setattr(sched.repo, "set_state", set_state)

    await sched.send_daily_digests(object(), day=date(2026, 9, 20))

    assert saved["digest:last_run"]["day"] == "2026-09-20"
    assert saved["digest:last_run"]["sent"] == 0


def test_local_today_follows_configured_timezone():
    """В 23:30 по Камчатке в UTC ещё позавчерашний день."""
    assert sched.local_today() == datetime.now(ZoneInfo("Asia/Kamchatka")).date()


# -------------------------------------------------------------- диспетчер

def test_dispatcher_wires_qa_router_with_owner_check():
    dp = build_dispatcher()
    names = [r.name for r in dp.sub_routers]

    assert "qa" in names
    qa = next(r for r in dp.sub_routers if r.name == "qa")
    kinds = {type(m).__name__ for m in qa.message.middleware}
    assert "OwnerOnly" in kinds and "RateLimit" in kinds


def test_digest_time_can_be_overridden():
    job = sched.build_scheduler(bot=object(), digest_time=time(9, 15)).get_job("daily_digest")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "9" and fields["minute"] == "15"
