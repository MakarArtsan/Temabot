"""Публикация дайджеста в саму группу: только по решению владельца.

Живой Telegram не нужен и не используется: бот подделан, «группа» — это id
в подставном боте. Интеграционные тесты идут на локальном Postgres.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from src.digest.publish import group_parts, publish_digest, unpublish_digest
from src.digest.render import DigestData, Topic

GROUP = -1002354231333
OTHER_GROUP = -1009999999999
OWNER = 132036441
DAY = date(2026, 9, 24)


class GroupBot:
    """Подставной Bot API: запоминает, что и куда ушло бы."""

    def __init__(self, *, fail_at: int | None = None, delete_fails: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.deleted: list[tuple[int, list[int]]] = []
        self.fail_at = fail_at
        self.delete_fails = delete_fails
        self._next_id = 500

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> Any:
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise RuntimeError("Forbidden: bot is not a member of the supergroup chat")
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=self._next_id)

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> bool:
        if self.delete_fails:
            raise RuntimeError("Bad Request: message can't be deleted for everyone")
        self.deleted.append((chat_id, list(message_ids)))
        return True


def digest_data(chat_tg_id: int = GROUP, *, topics: int = 2, heroes: str = "",
                long: bool = False) -> DigestData:
    body = "очень подробный вывод обсуждения " * (40 if long else 1)
    return DigestData(
        chat_tg_id=chat_tg_id,
        day=DAY,
        chat_title="Рабочая",
        highlights=["договорились о релизе в пятницу"] if topics else [],
        topics=[
            Topic(thread_id=n, title=f"Тема {n}", takeaway=f"{body} {n}", msg_count=5,
                  key_msg_ids=[n * 10])
            for n in range(1, topics + 1)
        ],
        msg_count=40,
        participants=6,
        heroes=heroes,
    )


# ============================================================ текст для группы

def test_group_text_has_no_heroes_without_ratings_consent():
    """«Герои дня» с именами — это рейтинг: без согласия группы наружу не идёт."""
    data = digest_data(heroes="🏅 Герои дня: 🧠 Вася")

    hidden = "\n".join(group_parts(data, ratings_public=False))
    shown = "\n".join(group_parts(data, ratings_public=True))

    assert "Герои дня" not in hidden
    assert "Герои дня" in shown
    assert "Тема 1" in hidden and "договорились о релизе" in hidden
    assert data.heroes, "исходные данные не трогаем"


def test_long_digest_is_split_for_telegram():
    parts = group_parts(digest_data(topics=30, long=True), ratings_public=False)

    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)


# ================================================================= с базой

@pytest.fixture
async def stored(db: None) -> Any:
    """Группа и дайджест за день в локальной базе."""
    from src.db import repo

    async def make(mode: str = "manual", data: DigestData | None = None,
                   chat_tg_id: int = GROUP) -> tuple[Any, int]:
        chat = await repo.get_or_create_chat(chat_tg_id, "Рабочая")
        chat = await repo.set_chat_flags(chat_tg_id, digest=True, publish=mode)
        payload = (data or digest_data(chat_tg_id)).to_dict()
        digest_id = await repo.save_digest(chat.id, DAY, "md", payload=payload, msg_count=40)
        return chat, digest_id

    return make


async def published_at(digest_id: int) -> Any:
    from src.db import repo

    digest = await repo.get_digest_by_id(digest_id)
    assert digest is not None
    return digest.published_at


async def test_new_group_never_publishes_by_default(db: None):
    from src.db import repo

    chat = await repo.bootstrap_primary_chat(GROUP)
    assert chat.publish == "off", "в группу ничего не уходит, пока владелец не включит"

    digest_id = await repo.save_digest(chat.id, DAY, "md", payload=digest_data().to_dict())
    bot = GroupBot()
    result = await publish_digest(bot, digest_id)

    assert not result.ok and "выключена" in result.text
    assert bot.sent == []
    assert await published_at(digest_id) is None


async def test_publish_goes_only_to_its_own_group(stored: Any):
    await stored("manual", chat_tg_id=OTHER_GROUP)
    _, digest_id = await stored("manual")
    bot = GroupBot()

    result = await publish_digest(bot, digest_id)

    assert result.ok, result.text
    assert {m["chat_id"] for m in bot.sent} == {GROUP}, "дайджест группы — только в неё же"
    assert all(m.get("reply_markup") is None for m in bot.sent), "кнопки оценки — не для группы"
    assert all(m["disable_notification"] for m in bot.sent), "поздно вечером — без звука"
    assert "Тема 1" in bot.sent[0]["text"]
    assert result.link.startswith("https://t.me/c/2354231333/")

    from src.db import repo

    digest = await repo.get_digest_by_id(digest_id)
    assert digest is not None and digest.published_at is not None
    assert digest.published_msg_ids == result.message_ids


async def test_second_publish_does_not_duplicate(stored: Any):
    _, digest_id = await stored("manual")
    bot = GroupBot()

    await publish_digest(bot, digest_id)
    again = await publish_digest(bot, digest_id)

    assert again.skipped and "уже опубликован" in again.text
    assert len(bot.sent) == 1


async def test_rebuilt_digest_keeps_publication_mark(stored: Any):
    """Пересборка дня не должна открыть дорогу второй публикации."""
    from src.db import repo

    chat, digest_id = await stored("auto")
    await publish_digest(GroupBot(), digest_id, auto=True)
    await repo.save_digest(chat.id, DAY, "md2", payload=digest_data().to_dict())

    assert await published_at(digest_id) is not None


async def test_auto_publication_needs_auto_mode(stored: Any):
    _, digest_id = await stored("manual")
    bot = GroupBot()

    result = await publish_digest(bot, digest_id, auto=True)

    assert result.skipped and bot.sent == []


async def test_empty_day_is_not_published(stored: Any):
    _, digest_id = await stored("auto", data=digest_data(topics=0))
    bot = GroupBot()

    result = await publish_digest(bot, digest_id, auto=True)

    assert result.skipped and bot.sent == []
    assert await published_at(digest_id) is None


async def test_failed_send_can_be_retried(stored: Any):
    """Бота нет в группе — отметка снимается, после исправления можно повторить."""
    _, digest_id = await stored("manual")

    failed = await publish_digest(GroupBot(fail_at=0), digest_id)
    assert not failed.ok and "не принял" in failed.text
    assert await published_at(digest_id) is None

    retried = await publish_digest(GroupBot(), digest_id)
    assert retried.ok


async def test_half_sent_digest_is_not_sent_again(stored: Any):
    _, digest_id = await stored("manual", data=digest_data(topics=30, long=True))

    partial = await publish_digest(GroupBot(fail_at=1), digest_id)
    assert not partial.ok and "не полностью" in partial.text
    assert len(partial.message_ids) == 1

    bot = GroupBot()
    again = await publish_digest(bot, digest_id)
    assert again.skipped and bot.sent == [], "начало уже в группе — не дублируем"


async def test_unpublish_removes_posts_and_allows_republishing(stored: Any):
    _, digest_id = await stored("manual")
    bot = GroupBot()
    first = await publish_digest(bot, digest_id)

    removed = await unpublish_digest(bot, digest_id)

    assert removed.ok
    assert bot.deleted == [(GROUP, first.message_ids)]
    assert await published_at(digest_id) is None
    assert (await publish_digest(bot, digest_id)).ok


async def test_unpublish_failure_keeps_the_mark(stored: Any):
    _, digest_id = await stored("manual")
    bot = GroupBot(delete_fails=True)
    await publish_digest(bot, digest_id)

    result = await unpublish_digest(bot, digest_id)

    assert not result.ok and "48 часов" in result.text
    assert await published_at(digest_id) is not None


async def test_unknown_publish_mode_is_rejected(db: None):
    import asyncpg
    from src.db import repo

    await repo.get_or_create_chat(GROUP, "Рабочая")
    with pytest.raises(asyncpg.CheckViolationError):
        await repo.set_chat_flags(GROUP, publish="everywhere")


async def test_digest_list_shows_publication(stored: Any):
    from src.db import repo

    _, digest_id = await stored("manual")
    await publish_digest(GroupBot(), digest_id)

    row = (await repo.list_recent_digests())[0]
    assert row["publish"] == "manual"
    assert row["published_at"] is not None and row["published_msg_ids"]


# ============================================================ расписание

@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    # тот же объект настроек, что видит модуль (test_config перезагружает src.config)
    from src.bot import handlers_publish

    monkeypatch.setattr(handlers_publish.cfg, "OWNER_ID", OWNER)


def chat_in(mode: str) -> Any:
    from src.db.models import Chat

    return Chat(id=1, tg_id=GROUP, title="Рабочая", digest=True, publish=mode)


async def test_scheduler_does_nothing_when_publication_is_off(
    monkeypatch: pytest.MonkeyPatch, owner: None
):
    from src.digest import publish as publish_mod
    from src.digest import scheduler as sched

    async def explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("в режиме «только мне» публиковать нельзя")

    monkeypatch.setattr(publish_mod, "publish_digest", explode)
    bot = GroupBot()

    await sched.publish_to_group(bot, chat_in("off"), 7)

    assert bot.sent == []


async def test_scheduler_auto_publishes_and_tells_the_owner(
    monkeypatch: pytest.MonkeyPatch, owner: None
):
    from src.digest import publish as publish_mod
    from src.digest import scheduler as sched
    from src.digest.publish import PublishResult

    calls: list[tuple[int, bool]] = []

    async def fake_publish(bot: Any, digest_id: int, *, auto: bool = False) -> PublishResult:
        calls.append((digest_id, auto))
        return PublishResult(True, "Опубликовал в «Рабочая»", message_ids=[501],
                             link="https://t.me/c/2354231333/501")

    monkeypatch.setattr(publish_mod, "publish_digest", fake_publish)
    bot = GroupBot()

    await sched.publish_to_group(bot, chat_in("auto"), 7)

    assert calls == [(7, True)]
    assert [m["chat_id"] for m in bot.sent] == [OWNER], "владельцу — отчёт о публикации"
    report = bot.sent[0]["text"]
    assert "Автопубликация" in report and "t.me/c/2354231333/501" in report


async def test_scheduler_skipped_auto_publication_is_silent(
    monkeypatch: pytest.MonkeyPatch, owner: None
):
    from src.digest import publish as publish_mod
    from src.digest import scheduler as sched
    from src.digest.publish import PublishResult

    async def fake_publish(bot: Any, digest_id: int, *, auto: bool = False) -> PublishResult:
        return PublishResult(False, "За день ничего заметного", skipped=True)

    monkeypatch.setattr(publish_mod, "publish_digest", fake_publish)
    bot = GroupBot()

    await sched.publish_to_group(bot, chat_in("auto"), 7)

    assert bot.sent == []


async def test_scheduler_offers_a_button_in_manual_mode(
    monkeypatch: pytest.MonkeyPatch, owner: None
):
    from src.db import repo
    from src.db.models import Digest
    from src.digest import scheduler as sched

    async def get_digest_by_id(digest_id: int) -> Digest:
        return Digest(id=digest_id, chat_id=1, day=DAY, summary_md="",
                      payload=digest_data().to_dict())

    monkeypatch.setattr(repo, "get_digest_by_id", get_digest_by_id)
    bot = GroupBot()

    await sched.publish_to_group(bot, chat_in("manual"), 7)

    assert [m["chat_id"] for m in bot.sent] == [OWNER], "в группу сам не пишет — только спрашивает"
    buttons = bot.sent[0]["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data == "pub:ask:7"


# ============================================================== кнопки в личке

class Edits:
    log: ClassVar[list[tuple[str, Any]]] = []


def owner_message() -> Any:
    from datetime import datetime

    from aiogram import types

    class EditableMessage(types.Message):
        async def edit_text(self, text: str, **kw: Any) -> None:  # type: ignore[override]
            Edits.log.append((text, kw.get("reply_markup")))

    return EditableMessage.model_construct(
        message_id=1, date=datetime.now(), chat=types.Chat(id=OWNER, type="private")
    )


class Callback:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.message = owner_message()

    async def answer(self, text: str | None = None, **kw: Any) -> None:
        self.answers.append(text or "")


async def test_button_asks_for_confirmation_first(monkeypatch: pytest.MonkeyPatch):
    from src.bot import handlers_publish as handlers
    from src.db.models import Chat, Digest

    async def get_digest_by_id(digest_id: int) -> Digest:
        return Digest(id=digest_id, chat_id=1, day=DAY, summary_md="",
                      payload=digest_data().to_dict())

    async def get_chat_by_id(chat_id: int) -> Chat:
        return chat_in("manual")

    async def explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("первое касание не публикует")

    monkeypatch.setattr(handlers.repo, "get_digest_by_id", get_digest_by_id)
    monkeypatch.setattr(handlers.repo, "get_chat_by_id", get_chat_by_id)
    monkeypatch.setattr(handlers, "publish_digest", explode)
    Edits.log = []

    await handlers.on_publish(Callback(), handlers.PublishCb(action="ask", digest_id=7), GroupBot())

    text, markup = Edits.log[-1]
    assert "Точно" in text and "Рабочая" in text
    data = [b.callback_data for b in markup.inline_keyboard[0]]
    assert data == ["pub:yes:7", "pub:no:7"]


async def test_confirmation_publishes(monkeypatch: pytest.MonkeyPatch):
    from src.bot import handlers_publish as handlers
    from src.digest.publish import PublishResult

    calls: list[int] = []

    async def fake_publish(bot: Any, digest_id: int, *, auto: bool = False) -> PublishResult:
        calls.append(digest_id)
        return PublishResult(True, "Опубликовал в «Рабочая»", message_ids=[501],
                             link="https://t.me/c/2354231333/501")

    monkeypatch.setattr(handlers, "publish_digest", fake_publish)
    Edits.log = []
    callback = Callback()

    await handlers.on_publish(callback, handlers.PublishCb(action="yes", digest_id=7), GroupBot())

    assert calls == [7]
    assert "Опубликовал" in Edits.log[-1][0] and "t.me/c/2354231333/501" in Edits.log[-1][0]


async def test_cancel_does_not_publish(monkeypatch: pytest.MonkeyPatch):
    from src.bot import handlers_publish as handlers

    async def explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("отмена не публикует")

    monkeypatch.setattr(handlers, "publish_digest", explode)
    Edits.log = []

    await handlers.on_publish(Callback(), handlers.PublishCb(action="no", digest_id=7), GroupBot())

    assert "Не публикую" in Edits.log[-1][0]


def test_publish_buttons_are_owner_only():
    from src.bot.main import build_dispatcher
    from src.bot.middlewares import OwnerOnly

    dp = build_dispatcher()
    publish = next(r for r in dp.sub_routers if r.name == "publish")

    assert any(isinstance(m, OwnerOnly) for m in publish.callback_query.middleware)
