"""Чанкование тредов для поиска (TZ §4.4).

Тред — естественная единица смысла, поэтому короткий тред идёт одним чанком.
Длинный режется окнами примерно по 800 токенов с перехлёстом в 100, чтобы
ответ не обрывался на границе.

Токены считаем приближённо: для русского это примерно 2.5 символа на токен.
Точный счётчик привязал бы нас к конкретному токенизатору, а здесь важен
порядок величины, а не точность.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.db.models import Message
from src.nlp.threads import Thread

CHARS_PER_TOKEN = 2.5
WINDOW_TOKENS = 800
OVERLAP_TOKENS = 100

WINDOW_CHARS = int(WINDOW_TOKENS * CHARS_PER_TOKEN)
OVERLAP_CHARS = int(OVERLAP_TOKENS * CHARS_PER_TOKEN)


@dataclass(slots=True)
class Chunk:
    """Кусок треда, который попадёт в индекс."""

    chat_id: int
    thread_id: int
    msg_ids: list[int] = field(default_factory=list)
    text: str = ""
    date_from: datetime | None = None
    date_to: datetime | None = None


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def _line(message: Message) -> str:
    author = message.author_name or (
        str(message.tg_user_id) if message.tg_user_id else "аноним"
    )
    body = message.content or (f"[{message.media_type}]" if message.media_type else "")
    return f"{author}: {body}"


def chunk_thread(
    thread: Thread, *, window_chars: int = WINDOW_CHARS, overlap_chars: int = OVERLAP_CHARS
) -> list[Chunk]:
    """Разложить тред по чанкам. Сообщение целиком попадает в один чанк."""
    messages = [m for m in thread.messages if (m.content or m.media_type)]
    if not messages:
        return []

    chunks: list[Chunk] = []
    current: list[Message] = []
    current_len = 0

    for message in messages:
        line = _line(message)
        if current and current_len + len(line) > window_chars:
            chunks.append(_build(thread, current))
            # перехлёст: последние сообщения уходят и в следующий чанк,
            # иначе ответ, разорванный границей, потеряется для поиска
            current, current_len = _tail(current, overlap_chars, window_chars)
        current.append(message)
        current_len += len(line) + 1

    if current:
        chunks.append(_build(thread, current))
    return chunks


def _tail(
    messages: list[Message], overlap_chars: int, window_chars: int
) -> tuple[list[Message], int]:
    """Хвост предыдущего чанка, который уйдёт в начало следующего.

    Последнее сообщение берём всегда, даже если оно длиннее бюджета перехлёста:
    в реальном чате одна реплика легко занимает сотни символов, и при жёстком
    бюджете перехлёста не было бы вовсе. Исключение — реплика длиннее целого
    окна: её повтор раздул бы следующий чанк вдвое.
    """
    tail: list[Message] = []
    size = 0
    for message in reversed(messages):
        line = _line(message)
        if not tail:
            if len(line) >= window_chars:
                break
        elif size + len(line) > overlap_chars:
            break
        tail.insert(0, message)
        size += len(line) + 1
        if size >= overlap_chars:
            break
    return tail, size


def _build(thread: Thread, messages: list[Message]) -> Chunk:
    return Chunk(
        chat_id=thread.chat_id,
        thread_id=thread.root_msg_id,
        msg_ids=[m.tg_msg_id for m in messages],
        text="\n".join(_line(m) for m in messages),
        date_from=messages[0].date,
        date_to=messages[-1].date,
    )


def chunk_threads(threads: list[Thread]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for thread in threads:
        chunks.extend(chunk_thread(thread))
    return chunks
