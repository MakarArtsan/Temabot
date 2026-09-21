"""Тесты бота-копировщика (TZ §4.6, шаг 9). Telegram и Telegraph не вызываются."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from src.bot import handlers_copier as copier
from src.bot.main import build_dispatcher

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
        reply_to_message=None, message_id=1,
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
    # упоминание вырезается «как есть»: в середине остаётся двойной пробел.
    # Это косметика в копируемом тексте, исходный файл ради неё не трогаем.
    assert copier._strip_mention("👋 @temabot текст") == "👋  текст"


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


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def mark_copied(**kw: Any) -> bool:
        calls.append(kw)
        return True

    monkeypatch.setattr(copier.repo, "mark_copied", mark_copied)
    monkeypatch.setattr(copier.cfg, "TG_GROUP_ID", GROUP)
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


async def test_reply_to_media_without_caption_explains(no_telegraph, no_db):
    parent = message("", mention=None, message_id=42)
    parent.text = None
    recorder = Recorder()
    await copier.on_mention(recorder.attach(message("@temabot", reply_to_message=parent)))

    assert no_telegraph == []


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
    monkeypatch.setattr(copier.cfg, "TG_GROUP_ID", GROUP)
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
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, **kw})


async def test_stranger_cannot_open_the_thread(monkeypatch: pytest.MonkeyPatch):
    """Содержимое закрытой группы не должно уходить никому, кроме владельца (§9)."""
    async def must_not_run(*a: Any, **kw: Any) -> str:
        raise AssertionError("чужой не должен добраться до контекста")

    monkeypatch.setattr(copier, "answer_about_thread", must_not_run)
    monkeypatch.setattr(copier.cfg, "OWNER_ID", OWNER)

    callback = FakeCallback(STRANGER)
    bot = FakeBot()
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=1), bot)

    assert callback.answers[0]["show_alert"] is True
    assert "владельцу" in callback.answers[0]["text"]
    assert bot.sent == [], "в группу и в личку ничего не ушло"


async def test_owner_gets_the_summary_in_private(monkeypatch: pytest.MonkeyPatch):
    async def summary(chat_tg_id: int, tg_msg_id: int) -> str:
        return "<b>Вокруг сообщения</b>\nкраткий пересказ"

    monkeypatch.setattr(copier, "answer_about_thread", summary)
    monkeypatch.setattr(copier.cfg, "OWNER_ID", OWNER)

    callback = FakeCallback(OWNER)
    bot = FakeBot()
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=5), bot)

    assert bot.sent[0]["chat_id"] == OWNER, "ответ уходит в личку, а не в группу"
    assert "краткий пересказ" in bot.sent[0]["text"]
    assert callback.answers[0]["text"] == "Собираю контекст, пришлю в личку"


async def test_summary_failure_does_not_crash(monkeypatch: pytest.MonkeyPatch):
    async def broken(*a: Any, **kw: Any) -> str:
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(copier, "answer_about_thread", broken)
    monkeypatch.setattr(copier.cfg, "OWNER_ID", OWNER)

    callback = FakeCallback(OWNER)
    await copier.on_around(callback, copier.AroundCb(chat_id=GROUP, msg_id=5), FakeBot())

    assert callback.answers, "пользователю всё равно ответили"


# ------------------------------------------------------------- порядок роутеров

def test_copier_router_goes_first():
    """Иначе перехватчик «любой текст — это вопрос» съел бы упоминания (TZ §4.6)."""
    names = [r.name for r in build_dispatcher().sub_routers]
    assert names == ["copier", "admin", "qa"]


def test_copier_is_public_but_gated_by_access_rules():
    """Копировщик работает для всех участников, но только в разрешённых группах."""
    build_dispatcher()
    kinds = {type(m).__name__ for m in copier.router.message.middleware}

    assert "OwnerOnly" not in kinds, "иначе копировщик перестал бы работать в группе"
    assert "CopierAccess" in kinds
    assert "CopierRateLimit" in kinds
