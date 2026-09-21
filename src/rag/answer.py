"""Ответы по истории чата (TZ §4.4, §4.6).

Правило одно: отвечать только по найденному контексту. Если данных нет —
так и сказать. Выдуманный ответ хуже отсутствия ответа: проверять его
читателю не по чему, кроме той же истории.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from src.config import cfg
from src.db import repo
from src.db.models import Message
from src.digest.render import deeplink, esc_html
from src.llm.client import Usage, chat
from src.rag.search import Hit, hybrid_search

log = logging.getLogger(__name__)

CONTEXT_RADIUS = 3      # ±3 соседних сообщения к каждому найденному куску (TZ §4.4)
MAX_CONTEXT_CHARS = 14_000
MAX_SOURCES = 5

ANSWER_SYSTEM = """\
Ты отвечаешь на вопросы по переписке закрытого рабочего чата.

Отвечай ТОЛЬКО по приведённому контексту. Если в нём нет ответа, так и напиши:
«В истории чата об этом нет». Не додумывай и не обобщай сверх написанного.

Пиши по-русски, коротко и по делу. Если в контексте есть конкретика — числа,
названия, цены, шаги — приводи её. Ссылайся на сообщения в квадратных скобках,
например [123], когда опираешься на конкретную реплику.
"""

ANSWER_USER = """\
Вопрос: {question}

Контекст из чата:
{context}
"""

THREAD_SYSTEM = """\
Ты коротко пересказываешь обсуждение из рабочего чата: о чём речь, к чему пришли,
что осталось открытым. Три-четыре предложения, по-русски, без вступлений.
Опирайся только на приведённые сообщения.
"""


@dataclass(slots=True)
class Source:
    chat_tg_id: int
    tg_msg_id: int
    author: str
    date: Any
    snippet: str

    def as_html(self) -> str:
        when = (
            self.date.astimezone(ZoneInfo(cfg.TZ)).strftime("%d.%m.%Y")
            if self.date
            else "—"
        )
        link = deeplink(self.chat_tg_id, self.tg_msg_id)
        return f'{when}, {esc_html(self.author)}: <a href="{link}">↗️</a>'


@dataclass(slots=True)
class Answer:
    text: str
    sources: list[Source] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    found: bool = True

    def as_html(self) -> str:
        parts = [esc_html(self.text)]
        if self.sources:
            parts.append("")
            parts.append("<b>Источники</b>")
            parts += [f"• {s.as_html()}" for s in self.sources]
        return "\n".join(parts)


def _line(message: Message) -> str:
    author = message.author_name or (
        str(message.tg_user_id) if message.tg_user_id else "аноним"
    )
    body = message.content or (f"[{message.media_type}]" if message.media_type else "")
    return f"[{message.tg_msg_id}] {author}: {body}"


async def _expand(hit: Hit) -> list[Message]:
    """Добрать соседние сообщения треда: найденный кусок редко самодостаточен."""
    thread = await repo.get_thread_messages(hit.chat_id, hit.thread_id, limit=40)
    if thread:
        return thread
    if hit.msg_ids:
        return await repo.get_messages_around(
            hit.chat_id, hit.msg_ids[0], radius=CONTEXT_RADIUS
        )
    return []


async def build_context(hits: list[Hit]) -> tuple[str, list[Message]]:
    """Собрать текст контекста и список сообщений, из которых он сложен."""
    seen: set[tuple[int, int]] = set()
    collected: list[Message] = []

    for hit in hits:
        for message in await _expand(hit):
            key = (message.chat_id, message.tg_msg_id)
            if key not in seen:
                seen.add(key)
                collected.append(message)

    collected.sort(key=lambda m: (m.date, m.tg_msg_id))

    lines: list[str] = []
    size = 0
    for message in collected:
        line = _line(message)
        if size + len(line) > MAX_CONTEXT_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines), collected[: len(lines)]


async def _sources_from(messages: list[Message], hits: list[Hit]) -> list[Source]:
    """Источники — ключевые сообщения найденных кусков, а не весь контекст."""
    wanted: list[int] = []
    for hit in hits:
        if hit.msg_ids:
            wanted.append(hit.msg_ids[0])

    by_id = {m.tg_msg_id: m for m in messages}
    chats: dict[int, int] = {}
    sources: list[Source] = []

    for msg_id in wanted[:MAX_SOURCES]:
        message = by_id.get(msg_id)
        if message is None:
            continue
        if message.chat_id not in chats:
            group = await repo.get_chat_by_id(message.chat_id)
            chats[message.chat_id] = group.tg_id if group else 0
        sources.append(
            Source(
                chat_tg_id=chats[message.chat_id],
                tg_msg_id=message.tg_msg_id,
                author=message.author_name or "аноним",
                date=message.date,
                snippet=(message.content or "")[:120],
            )
        )
    return sources


async def answer_question(question: str, *, chat_id: int | None = None) -> Answer:
    """Ответ по истории чата с источниками (TZ §4.4)."""
    hits = await hybrid_search(question, chat_id=chat_id)
    if not hits:
        return Answer(text="В истории чата об этом нет.", found=False)

    context, messages = await build_context(hits)
    if not context:
        return Answer(text="В истории чата об этом нет.", found=False)

    reply = await chat(
        [
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": ANSWER_USER.format(question=question, context=context)},
        ],
        purpose="qa",
        chat_id=chat_id,
    )

    sources = await _sources_from(messages, hits)
    answer = Answer(text=reply.text.strip(), sources=sources, usage=reply.usage)

    try:
        await repo.log_qa(question, answer.text, [s.tg_msg_id for s in sources])
    except Exception:
        log.warning("Не удалось записать вопрос в qa_log", exc_info=True)
    return answer


async def answer_about_thread(chat_tg_id: int, tg_msg_id: int) -> str:
    """«Что обсуждали вокруг» для кнопки копировщика (TZ §4.6).

    Возвращает HTML с разрешёнными Telegram тегами и дип-линками.
    """
    group = await repo.get_chat_by_tg_id(chat_tg_id)
    if group is None:
        return "Это сообщение не из отслеживаемой группы."

    message = await repo.get_message(group.id, tg_msg_id)
    if message is None:
        return "Такого сообщения нет в базе."

    if message.thread_id:
        around = await repo.get_thread_messages(group.id, message.thread_id, limit=40)
    else:
        around = await repo.get_messages_around(group.id, tg_msg_id, radius=20)

    if not around:
        return "Вокруг этого сообщения ничего не нашлось."

    context = "\n".join(_line(m) for m in around)[:MAX_CONTEXT_CHARS]
    reply = await chat(
        [
            {"role": "system", "content": THREAD_SYSTEM},
            {"role": "user", "content": context},
        ],
        purpose="qa",
        chat_id=group.id,
    )

    link = deeplink(chat_tg_id, tg_msg_id)
    lines = [
        f'<b>Вокруг сообщения</b> <a href="{link}">↗️</a>',
        "",
        esc_html(reply.text.strip()),
        "",
        f"<i>сообщений в обсуждении: {len(around)}</i>",
    ]
    return "\n".join(lines)
