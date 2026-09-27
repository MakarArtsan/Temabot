"""Тесты бота-копировщика (TZ §4.6, шаг 9). Telegram и Telegraph не вызываются."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramForbiddenError
from src.bot import handlers_copier as copier
from src.bot.main import build_dispatcher
from src.bot.middlewares import RateLimit
from src.db.models import Chat

OWNER = 132036441
STRANGER = 999999
GROUP = -1002354231333
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class Entity:
    """Сущность сообщения Telegram: extract_from умеет считать offset в UTF-16."""

    def __init__(self, text: str, kind: str, offset: int, length: int) -> None:
        self.type = kind
        self.offset = offset
        self.length = length
        self._full = text

    def extract_from(self, text: str) -> str:
        utf16 = text.encode("utf-16-le")
        return utf16[self.offset * 2 : (self.offset + self.length) * 2].decode("utf-16-le")


def mention_entities(text: str, mention: str) -> list[Entity]:
    """Разметить упоминание так, как это делает Telegram — в кодовых единицах UTF-16."""
    index = text.index(mention)
    offset = len(text[:index].encode("utf-16-le")) // 2
    length = len(mention.encode("utf-16-le")) // 2
    return [Entity(text, "mention", offset, length)]


def message(text: str, *, mention: str | None = "@temabot", **kw: Any) -> SimpleNamespace:
    entities = mention_entities(text, mention) if mention and mention in text else []
    defaults: dict[str, Any] = dict(
        text=text, caption=None, entities=entities, caption_entities=None,
        reply_to_message=None, message_id=1, bot=None,
        chat=SimpleNamespace(id=GROUP), from_user=SimpleNamespace(id=OWNER, full_name="Вася"),
        date=NOW,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def bot_username(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(copier, "_bot_username", "temabot")


# ------------------------------------------------------ распознавание упоминания

async def test_mention_is_recognised():
    assert await copier.MentionsMe()(message("@temabot скопируй это")) is True


async def test_emoji_before_mention_does_not_break_offsets():
    """Telegram считает offset в UTF-16: эмодзи занимает две единицы, а не одну.

    Ручной срез строки по offset ломался именно на этом — ради этого в исходнике
    и заменили его на entity.extract_from.
    """
    text = "👋 @temabot скопируй это"
    assert await copier.MentionsMe()(message(text)) is True


async def test_several_emoji_before_mention():
    text = "🔥🔥🔥 смотри 👋 @temabot вот этот текст"
    assert await copier.MentionsMe()(message(text)) is True


async def test_other_bot_mention_is_ignored():
    text = "@otherbot скопируй"
    assert await copier.MentionsMe()(message(text, mention="@otherbot")) is False


async def test_message_without_mention():
    assert await copier.MentionsMe()(message("просто текст", mention=None)) is False


async def test_mention_in_caption():
    msg = message("", mention=None)
    msg.text = None
    msg.caption = "@temabot скопируй подпись"
    msg.caption_entities = mention_entities(msg.caption, "@temabot")
    assert await copier.MentionsMe()(msg) is True


def test_strip_mention_keeps_the_rest():
    assert copier._strip_mention("@temabot скопируй это") == "скопируй это"
    # в середине текста упоминание не должно оставлять двойной пробел
    assert copier._strip_mention("👋 @temabot текст") == "👋 текст"
    assert copier._strip_mention("до @temabot после") == "до после"


# ------------------------------------------------------------ что копируем

class Recorder:
    """Перехватывает ответы вместо отправки в Telegram."""

    def __init__(self) -> None:
        self.replies: list[dict[str, Any]] = []

    def attach(self, msg: SimpleNamespace) -> SimpleNamespace:
        async def reply(text: str, **kw: Any) -> None:
            self.replies.append({"text": text, **kw})

        msg.reply = reply
        return msg


@pytest.fixture
def no_telegraph(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    pages: list[str] = []

    async def make_page(text: str) -> str:
        pages.append(text)
        return "https://telegra.ph/test"

    monkeypatch.setattr(copier, "_make_page", make_page)
    return pages


@pytest.fixture(autouse=True)
def groups(monkeypatch: pytest.MonkeyPatch) -> dict[int, Chat]:
    """Настройки групп вместо базы: GROUP читается сборщиком, копировщик разрешён."""
    known = {GROUP: Chat(id=1, tg_id=GROUP, title="Группа", collect=True, copier="allow")}
    blocked: set[int] = set()

    async def chat(chat_tg_id: int, **kw: Any) -> Chat | None:
        return known.get(chat_tg_id)

    async def blocked_ids(**kw: Any) -> set[int]:
        return blocked

    monkeypatch.setattr(copier.settings_cache, "chat", chat)
    monkeypatch.setattr(copier.settings_cache, "blocked", blocked_ids)
    monkeypatch.setattr(copier.cfg, "OWNER_ID", OWNER)
    monkeypatch.setattr(copier, "around_limit", RateLimit(limit=3, window_sec=600.0))
    copier._around_cache.clear()
    return known


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def mark_copied(**kw: Any) -> bool:
        calls.append(kw)
        return True

    monkeypatch.setattr(copier.repo, "mark_copied", mark_copied)
    return calls


async def test_text_after_mention_is_copied(no_telegraph, no_db):
    recorder = Recorder()
    msg = recorder.attach(message("@temabot вот этот текст"))

    await copier.on_mention(msg)

    assert no_telegraph == ["вот этот текст"]
    assert recorder.replies[0]["text"] == "Готово!"


async def test_reply_without_text_copies_the_parent(no_telegraph, no_db):
    """`@bot` реплаем на чужое сообщение копирует то сообщение (TZ §4.6)."""
    parent = message("важный текст Пети", mention=None, message_id=42,
                     from_user=SimpleNamespace(id=777, full_name="Петя"))
    recorder = Recorder()
    msg = recorder.attach(message("@temabot", reply_to_message=parent, message_id=43))

    await copier.on_mention(msg)

    assert no_telegraph == ["важный текст Пети"]
    assert no_db[0]["tg_msg_id"] == 42, "в базе отмечаем родительское сообщение"
    assert no_db[0]["tg_user_id"] == 777
    assert no_db[0]["author_name"] == "Петя"


async def test_own_text_wins_over_reply(no_telegraph, no_db):
    parent = message("текст родителя", mention=None, message_id=42)
    recorder = Recorder()
    msg = recorder.attach(message("@temabot свой текст", reply_to_message=parent))

    await copier.on_mention(msg)

    assert no_telegraph == ["свой текст"]


async def test_mention_without_text_and_without_reply_explains(no_telegraph, no_db):
    recorder = Recorder()
    await copier.on_mention(recorder.attach(message("@temabot")))

    assert no_telegraph == [], "страницу не создаём"
    assert "ответь упоминанием" in recorder.replies[0]["text"]


async def test_reply_to_media_without_caption_explains(
    no_telegraph, no_db, groups: dict[int, Chat],
):
    """Ответ на фото без подписи — это попытка скопировать, а не запрос дайджеста."""
    groups[GROUP].publish = "auto"
    parent = message("", mention=None, message_id=42)
    parent.text = None
    recorder = Recorder()
    await copier.on_mention(recorder.attach(message("@temabot", reply_to_message=parent)))

    assert no_telegraph == []
    assert "нет текста" in recorder.replies[0]["text"]


# ------------------------------------------- упоминание без текста — дайджест

@pytest.fixture
def digests(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Архив группы: свежий неопубликованный и опубликованный вчерашний."""
    from src.db.models import Digest
    from src.digest import publish
    from src.digest.render import DigestData, Topic

    def payload(day: Any, title: str) -> dict[str, Any]:
        return DigestData(
            chat_tg_id=GROUP, day=day, chat_title="Группа",
            highlights=[f"Главное: {title}"],
            topics=[Topic(thread_id=11, title=title, kind="insight", msg_count=9,
                          key_msg_ids=[5])],
            msg_count=50, participants=5,
        ).to_dict()

    from datetime import date
    archive = [
        Digest(id=2, chat_id=1, day=date(2026, 9, 27), summary_md="",
               payload=payload(date(2026, 9, 27), "Свежая тема")),
        Digest(id=1, chat_id=1, day=date(2026, 9, 26), summary_md="",
               payload=payload(date(2026, 9, 26), "Вчерашняя тема"), published_at=NOW),
    ]

    async def list_chat_digests(chat_id: int, limit: int = 60) -> list[Any]:
        return archive

    async def site_base() -> str:
        return "https://site.example.com"

    async def issue_number(chat_id: int, day: Any) -> int:
        return 1 if day.day == 26 else 2

    monkeypatch.setattr(publish.repo, "list_chat_digests", list_chat_digests)
    monkeypatch.setattr(publish.repo, "digest_issue_number", issue_number)
    monkeypatch.setattr(publish, "site_base", site_base)
    copier._digest_shown.clear()
    return archive


def bare_mention() -> SimpleNamespace:
    return message("@temabot", chat=SimpleNamespace(id=GROUP, type="supergroup"),
                   from_user=SimpleNamespace(id=STRANGER, full_name="Участник"))


async def test_bare_mention_shows_the_last_published_digest(
    no_telegraph, no_db, digests: list[Any], groups: dict[int, Chat],
):
    """«По кнопке»: непроверенный владельцем выпуск по запросу в группу не уходит."""
    groups[GROUP].publish = "manual"
    groups[GROUP].portal = "all"
    recorder = Recorder()
    await copier.on_mention(recorder.attach(bare_mention()))

    reply = recorder.replies[0]
    assert "Выпуск #1: Вчерашняя тема" in reply["text"] and "Свежая тема" not in reply["text"]
    assert "#дайджест" in reply["text"] and reply["parse_mode"] == "HTML"
    assert "https://site.example.com/g/1/d/2026-09-26" in reply["text"]
    assert no_telegraph == [], "ничего не копируем"


async def test_bare_mention_in_auto_mode_shows_the_newest(
    no_telegraph, no_db, digests: list[Any], groups: dict[int, Chat],
):
    groups[GROUP].publish = "auto"
    recorder = Recorder()
    await copier.on_mention(recorder.attach(bare_mention()))
    text = recorder.replies[0]["text"]
    assert "Свежая тема" in text
    # страница участников закрыта — анонс без ссылки в никуда
    assert "site.example.com" not in text and "#дайджест" in text


async def test_no_digest_where_publishing_is_off(
    no_telegraph, no_db, digests: list[Any], groups: dict[int, Chat],
):
    groups[GROUP].publish = "off"
    recorder = Recorder()
    await copier.on_mention(recorder.attach(bare_mention()))
    assert "ответь упоминанием" in recorder.replies[0]["text"]
    assert "#дайджест" not in recorder.replies[0]["text"]


async def test_digest_on_mention_is_not_spammed(
    no_telegraph, no_db, digests: list[Any], groups: dict[int, Chat],
):
    groups[GROUP].publish = "auto"
    recorder = Recorder()
    for _ in range(3):
        await copier.on_mention(recorder.attach(bare_mention()))
    assert ["#дайджест" in r["text"] for r in recorder.replies] == [True, False, False]
    assert "чуть выше" in recorder.replies[1]["text"]


async def test_bare_mention_inside_a_forum_topic_shows_the_digest(
    no_telegraph, no_db, digests: list[Any], groups: dict[int, Chat],
):
    """В теме форума Telegram шлёт сообщение «ответом» на служебное «тема создана»."""
    groups[GROUP].publish = "auto"
    topic_root = message("", mention=None, message_id=9, forum_topic_created=object())
    topic_root.text = None
    msg = bare_mention()
    msg.reply_to_message = topic_root
    msg.is_topic_message = True
    msg.message_thread_id = 9
    recorder = Recorder()
    await copier.on_mention(recorder.attach(msg))

    assert "#дайджест" in recorder.replies[0]["text"]
    assert "нет текста" not in recorder.replies[0]["text"]


async def test_topic_root_without_marker_is_not_a_reply(
    no_telegraph, no_db, digests: list[Any], groups: dict[int, Chat],
):
    groups[GROUP].publish = "auto"
    topic_root = message("", mention=None, message_id=9)
    topic_root.text = None
    msg = bare_mention()
    msg.reply_to_message = topic_root
    msg.is_topic_message = True
    msg.message_thread_id = 9
    recorder = Recorder()
    await copier.on_mention(recorder.attach(msg))
    assert "#дайджест" in recorder.replies[0]["text"]


async def test_real_reply_inside_a_topic_is_still_copied(no_telegraph, no_db):
    parent = message("текст соседа", mention=None, message_id=42)
    msg = message("@temabot", reply_to_message=parent, is_topic_message=True,
                  message_thread_id=9)
    recorder = Recorder()
    await copier.on_mention(recorder.attach(msg))
    assert no_telegraph == ["текст соседа"]


async def test_nothing_published_yet_is_explained(
    no_telegraph, no_db, digests: list[Any], groups: dict[int, Chat],
):
    groups[GROUP].publish = "manual"
    for d in digests:
        d.published_at = None
    recorder = Recorder()
    await copier.on_mention(recorder.attach(bare_mention()))
    assert "Опубликованных дайджестов пока нет" in recorder.replies[0]["text"]


async def test_telegraph_failure_does_not_crash(monkeypatch: pytest.MonkeyPatch, no_db):
    async def broken(text: str) -> str:
        raise ConnectionError("Telegraph недоступен")

    monkeypatch.setattr(copier, "_make_page", broken)
    recorder = Recorder()

    await copier.on_mention(recorder.attach(message("@temabot текст")))

    assert "что-то пошло не так" in recorder.replies[0]["text"].lower()


async def test_database_failure_does_not_break_copying(
    no_telegraph, monkeypatch: pytest.MonkeyPatch
):
    """Ошибка БД не должна ломать копирование (TZ §4.6)."""
    async def broken(**kw: Any) -> bool:
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(copier.repo, "mark_copied", broken)
    recorder = Recorder()

    await copier.on_mention(recorder.attach(message("@temabot текст")))

    assert recorder.replies[0]["text"] == "Готово!", "ссылка всё равно выдана"


async def test_foreign_chat_is_not_written_to_database(no_telegraph, no_db):
    """Копировать можно где угодно, но помечаем только отслеживаемую группу."""
    recorder = Recorder()
    msg = recorder.attach(message("@temabot текст", chat=SimpleNamespace(id=-100999)))

    await copier.on_mention(msg)

    assert no_telegraph == ["текст"]
    assert no_db == []


# --------------------------------------------------- кнопка «что обсуждали вокруг»

class FakeCallback:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, **kw: Any) -> None:
        self.answers.append({"text": text, **kw})


class FakeBot:
    def __init__(self, *, dm_closed: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.dm_closed = dm_closed

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        if self.dm_closed:
            raise TelegramForbiddenError(method=None, message="bot can't initiate conversation")  # type: ignore[arg-type]
        self.sent.append({"chat_id": chat_id, "text": text, **kw})


def summary_counter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    async def summary(chat_tg_id: int, tg_msg_id: int) -> str:
        calls.append((chat_tg_id, tg_msg_id))
        return "<b>Вокруг сообщения</b>\nкраткий пересказ"

    monkeypatch.setattr(copier, "answer_about_thread", summary)
    return calls


async def test_member_gets_the_summary_in_private(monkeypatch: pytest.MonkeyPatch):
    """Решение владельца: сводку получает любой участник группы — себе в личку."""
    summary_counter(monkeypatch)
    callback = FakeCallback(STRANGER)
    bot = FakeBot()
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=5), bot)

    assert [m["chat_id"] for m in bot.sent] == [STRANGER, STRANGER], "в личку, не в группу"
    assert "краткий пересказ" in bot.sent[-1]["text"]
    assert callback.answers[0]["text"] == "Пришлю в личку"


async def test_owner_gets_the_summary_in_private(monkeypatch: pytest.MonkeyPatch):
    summary_counter(monkeypatch)
    callback = FakeCallback(OWNER)
    bot = FakeBot()
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=5), bot)
    assert bot.sent[-1]["chat_id"] == OWNER and "краткий пересказ" in bot.sent[-1]["text"]


async def test_group_without_copier_gives_nothing(
    monkeypatch: pytest.MonkeyPatch, groups: dict[int, Chat],
):
    calls = summary_counter(monkeypatch)
    groups[GROUP].copier = "deny"
    callback = FakeCallback(STRANGER)
    bot = FakeBot()
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=1), bot)

    assert callback.answers[0]["show_alert"] is True
    assert bot.sent == [] and calls == []


async def test_unknown_group_gives_nothing(monkeypatch: pytest.MonkeyPatch):
    calls = summary_counter(monkeypatch)
    callback = FakeCallback(STRANGER)
    await copier.on_around(callback, copier.AroundCb(chat_id=-100999, msg_id=1), FakeBot())
    assert callback.answers[0]["show_alert"] is True and calls == []


async def test_closed_private_chat_asks_to_press_start(monkeypatch: pytest.MonkeyPatch):
    """Бот не может написать первым — человек узнаёт, что делать, а модель не зовём."""
    calls = summary_counter(monkeypatch)
    callback = FakeCallback(STRANGER)
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=1),
                           FakeBot(dm_closed=True))

    assert callback.answers[0]["show_alert"] is True
    assert "Старт" in callback.answers[0]["text"]
    assert calls == []


async def test_members_are_rate_limited_and_share_one_summary(monkeypatch: pytest.MonkeyPatch):
    calls = summary_counter(monkeypatch)
    bot = FakeBot()
    for _ in range(4):
        callback = FakeCallback(STRANGER)
        await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=7), bot)
    assert "Слишком часто" in callback.answers[0]["text"]
    assert calls == [(GROUP, 7)], "одна сводка на сообщение, дальше — из памяти"


async def test_blocked_user_gives_nothing(monkeypatch: pytest.MonkeyPatch):
    calls = summary_counter(monkeypatch)

    async def blocked(**kw: Any) -> set[int]:
        return {STRANGER}

    monkeypatch.setattr(copier.settings_cache, "blocked", blocked)
    callback = FakeCallback(STRANGER)
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=1), FakeBot())
    assert callback.answers[0]["show_alert"] is True and calls == []


async def test_summary_failure_does_not_crash(monkeypatch: pytest.MonkeyPatch):
    async def broken(*a: Any, **kw: Any) -> str:
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(copier, "answer_about_thread", broken)
    callback = FakeCallback(OWNER)
    bot = FakeBot()
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=5), bot)

    assert callback.answers, "пользователю всё равно ответили"
    assert "Не получилось" in bot.sent[-1]["text"]


# ------------------------------------------------------------- порядок роутеров

def test_copier_router_goes_first():
    """Иначе перехватчик «любой текст — это вопрос» съел бы упоминания (TZ §4.6)."""
    names = [r.name for r in build_dispatcher().sub_routers]
    assert names == ["copier", "admin", "feedback", "publish", "ratings", "qa"]
    assert names[-1] == "qa", "перехватчик любого текста — последним"


def test_copier_is_public_but_gated_by_access_rules():
    """Копировщик работает для всех участников, но только в разрешённых группах."""
    build_dispatcher()
    kinds = {type(m).__name__ for m in copier.router.message.middleware}

    assert "OwnerOnly" not in kinds, "иначе копировщик перестал бы работать в группе"
    assert "CopierAccess" in kinds
    assert "CopierRateLimit" in kinds


# ----------------------------------------------- кнопка «вокруг» — участникам

async def test_members_see_the_thread_button(no_telegraph, no_db):
    """Решение владельца: сводку может попросить любой участник группы."""
    recorder = Recorder()
    msg = recorder.attach(message(
        "@temabot текст", from_user=SimpleNamespace(id=STRANGER, full_name="Участник")
    ))

    await copier.on_mention(msg)

    buttons = [
        b.text for row in recorder.replies[0]["reply_markup"].inline_keyboard for b in row
    ]
    assert buttons == ["📄 Копировать текст", "🧵 Что обсуждали вокруг"]


async def test_untracked_group_has_no_thread_button(no_telegraph, no_db):
    recorder = Recorder()
    await copier.on_mention(recorder.attach(
        message("@temabot текст", chat=SimpleNamespace(id=-100999))
    ))

    buttons = [
        b.text for row in recorder.replies[0]["reply_markup"].inline_keyboard for b in row
    ]
    assert buttons == ["📄 Копировать текст"], "сводку делать не из чего"


# ------------------------------------------------------------- режим dm

async def test_dm_mode_does_not_touch_telegraph(
    no_telegraph, no_db, monkeypatch: pytest.MonkeyPatch
):
    """Содержимое закрытой группы не должно уходить на внешний сервис (TZ §4.6)."""
    monkeypatch.setattr(copier.cfg, "COPY_MODE", "dm")
    sent: list[dict[str, Any]] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
            sent.append({"chat_id": chat_id, "text": text})

    recorder = Recorder()
    msg = recorder.attach(message("@temabot секретный текст"))
    msg.bot = FakeBot()

    await copier.on_mention(msg)

    assert no_telegraph == [], "страница не создавалась"
    assert sent and sent[0]["chat_id"] == OWNER, "текст ушёл в личку запросившему"
    assert "секретный текст" in sent[0]["text"]
    assert "личку" in recorder.replies[0]["text"]


async def test_dm_mode_falls_back_when_user_never_started_the_bot(
    no_telegraph, no_db, monkeypatch: pytest.MonkeyPatch
):
    from aiogram.exceptions import TelegramForbiddenError

    monkeypatch.setattr(copier.cfg, "COPY_MODE", "dm")

    class ClosedBot:
        async def send_message(self, *a: Any, **kw: Any) -> None:
            raise TelegramForbiddenError(method=None, message="bot was blocked")

    recorder = Recorder()
    msg = recorder.attach(message("@temabot текст"))
    msg.bot = ClosedBot()

    await copier.on_mention(msg)

    assert no_telegraph == [], "в закрытую личку не отправили и наружу не выложили"
    assert "/start" in recorder.replies[0]["text"]
