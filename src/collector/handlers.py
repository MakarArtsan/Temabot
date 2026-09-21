"""События userbot: NewMessage / MessageEdited / MessageDeleted (TZ §4.1).

Нормализация вынесена в чистую функцию `normalize_message`: она не дёргает сеть
и не знает про БД, поэтому проверяется тестами без живого Telegram.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.db import repo
from src.db.models import Message

log = logging.getLogger(__name__)

# Порядок важен: video_note и voice — тоже документы, их надо поймать раньше.
_MEDIA_ATTRS = ("voice", "video_note", "audio", "photo", "sticker", "gif", "video", "document")

MEDIA_TYPE_MAP = {
    "voice": "voice",
    "video_note": "voice",   # кружок расшифровываем так же, как голосовое
    "audio": "audio",
    "photo": "photo",
    "sticker": "sticker",
    "gif": "gif",
    "video": "video",
    "document": "doc",
}


def detect_media_type(message: Any) -> str | None:
    """voice|audio|photo|video|doc|sticker|gif|None (TZ §3, колонка media_type)."""
    for attr in _MEDIA_ATTRS:
        if getattr(message, attr, None):
            return MEDIA_TYPE_MAP[attr]
    return None


def count_reactions(message: Any) -> int:
    """Сумма реакций: в скоринге это сигнал вовлечённости (TZ §4.7)."""
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", None) if reactions else None
    if not results:
        return 0
    return sum(int(getattr(r, "count", 0) or 0) for r in results)


def extract_thread_refs(message: Any) -> tuple[int | None, int | None]:
    """Вернуть (reply_to, topic_id) с учётом форумных тем.

    В форумной группе у сообщения, отправленного в тему без ответа кому-либо,
    reply_to_msg_id указывает на корень темы. Это не ответ, и считать его
    ответом нельзя — иначе вся тема склеится в одну reply-цепочку (TZ §4.2).
    """
    reply_to_obj = getattr(message, "reply_to", None)
    if reply_to_obj is None:
        return getattr(message, "reply_to_msg_id", None), None

    reply_to = getattr(reply_to_obj, "reply_to_msg_id", None)
    if not getattr(reply_to_obj, "forum_topic", False):
        return reply_to, None

    top_id = getattr(reply_to_obj, "reply_to_top_id", None)
    if top_id:
        return reply_to, top_id
    # без reply_to_top_id сообщение просто лежит в теме, а не отвечает на корень
    return None, reply_to


def author_display_name(sender: Any) -> str | None:
    """Имя автора: человек, канал или бот — у всех разные поля."""
    if sender is None:
        return None
    title = getattr(sender, "title", None)
    if title:
        return str(title)
    parts = [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    username = getattr(sender, "username", None)
    return f"@{username}" if username else None


def _raw_snapshot(message: Any) -> dict[str, Any]:
    """Компактный слепок: полный raw раздул бы базу на сотнях тысяч сообщений."""
    snapshot: dict[str, Any] = {}
    if getattr(message, "fwd_from", None):
        snapshot["forwarded"] = True
    for attr in ("grouped_id", "views", "post_author"):
        value = getattr(message, attr, None)
        if value:
            snapshot[attr] = value
    if getattr(message, "pinned", False):
        snapshot["pinned"] = True
    return snapshot


def normalize_message(
    message: Any, chat_id: int, *, author_name: str | None = None, source: str = "collector"
) -> Message:
    """Telethon-сообщение -> строка таблицы messages. Сети и БД не касается."""
    reply_to, topic_id = extract_thread_refs(message)
    text = getattr(message, "message", None) or None
    date = getattr(message, "date", None) or datetime.now(UTC)

    return Message(
        chat_id=chat_id,
        tg_msg_id=int(message.id),
        tg_user_id=getattr(message, "sender_id", None),
        author_name=author_name,
        text=text,
        media_type=detect_media_type(message),
        reply_to=reply_to,
        topic_id=topic_id,
        source=source,
        reactions=count_reactions(message),
        date=date,
        edited_at=getattr(message, "edit_date", None),
        raw=_raw_snapshot(message),
    )


class AuthorCache:
    """Кэш `tg_user_id -> имя`, обновляется раз в сутки (TZ §4.1)."""

    def __init__(self, ttl_sec: int = 24 * 3600) -> None:
        self._names: dict[int, str | None] = {}
        self._seen_at: dict[int, datetime] = {}
        self._ttl = ttl_sec

    def is_fresh(self, user_id: int, now: datetime | None = None) -> bool:
        seen = self._seen_at.get(user_id)
        if seen is None:
            return False
        return ((now or datetime.now(UTC)) - seen).total_seconds() < self._ttl

    def get(self, user_id: int) -> str | None:
        return self._names.get(user_id)

    def put(self, user_id: int, name: str | None, now: datetime | None = None) -> None:
        self._names[user_id] = name
        self._seen_at[user_id] = now or datetime.now(UTC)

    async def resolve(self, message: Any, *, persist: bool = True) -> str | None:
        """Имя автора: из кэша, иначе спросить у Telethon и запомнить."""
        user_id = getattr(message, "sender_id", None)
        if user_id is None:
            return None
        if self.is_fresh(user_id):
            return self.get(user_id)

        name: str | None = None
        try:
            sender = await message.get_sender()
            name = author_display_name(sender)
        except Exception:  # имя не критично, сообщение важнее
            log.warning("Не удалось получить автора %s", user_id, exc_info=True)
            return self.get(user_id)

        self.put(user_id, name)
        if persist:
            await repo.upsert_author(user_id, name)
        return name
