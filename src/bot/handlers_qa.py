"""Команды владельца в личке (TZ §4.5).

`/ask` и ответ на произвольный текст появятся на шаге 8 вместе с RAG.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Router, types
from aiogram.filters import Command, CommandObject

from src.config import cfg
from src.db import repo
from src.db.models import Chat
from src.digest import pipeline as digest_pipeline
from src.digest import prompts
from src.digest.render import DigestData, deeplink, esc_html, render_html, split_message
from src.llm.client import chat_json

log = logging.getLogger(__name__)
router = Router(name="qa")

HELP = """\
<b>Что я умею</b>

/digest — дайджест за сегодня
/digest 2026-09-15 — за конкретный день
/week — сводка за 7 дней
/search запрос — 10 сообщений со ссылками, без обращения к модели
/topics — темы за 30 дней
/who имя — чем занят участник
/pin — ответом на сообщение: пометить важным
/stats — что собрано и сколько потрачено

В группе меня можно упомянуть, чтобы скопировать текст.\
"""


def fmt_local(moment: datetime | None, pattern: str = "%d.%m %H:%M") -> str:
    """Время в таймзоне чата. В UTC пользователь видел бы вчерашний вечер."""
    if moment is None:
        return "—"
    return moment.astimezone(ZoneInfo(cfg.TZ)).strftime(pattern)


async def send_long(message: types.Message, text: str) -> None:
    """Дайджест активного дня не влезает в одно сообщение Telegram."""
    for part in split_message(text):
        await message.answer(
            part, parse_mode="HTML", link_preview_options=types.LinkPreviewOptions(is_disabled=True)
        )


async def _digest_chats() -> list[Chat]:
    chats = await repo.list_chats(digest=True)
    if chats:
        return chats
    # первый запуск: дайджест ещё не включён ни у кого
    if cfg.TG_GROUP_ID:
        found = await repo.get_chat_by_tg_id(cfg.TG_GROUP_ID)
        return [found] if found else []
    return []


@router.message(Command("start"))
async def on_start(message: types.Message) -> None:
    await message.answer(
        "Привет! Я собираю дайджест закрытой группы и отвечаю по её истории.\n\n" + HELP,
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def on_help(message: types.Message) -> None:
    await message.answer(HELP, parse_mode="HTML")


@router.message(Command("digest"))
async def on_digest(message: types.Message, command: CommandObject) -> None:
    """Дайджест за сегодня или за указанную дату (TZ §4.5)."""
    raw = (command.args or "").strip()
    try:
        day = date_type.fromisoformat(raw) if raw else _today()
    except ValueError:
        await message.answer("Дату нужно писать как 2026-09-15")
        return

    chats = await _digest_chats()
    if not chats:
        await message.answer("Ни для одной группы дайджест не включён.")
        return

    for chat in chats:
        stored = await repo.get_digest(chat.id, day)
        if stored and stored.payload:
            # готовый дайджест собираем заново из сохранённой структуры
            await send_long(message, render_html(DigestData.from_dict(stored.payload)))
            continue

        await message.answer(f"Собираю дайджест «{chat.title or chat.tg_id}» за {day}…")
        try:
            result = await digest_pipeline.run_for_chat(chat, day)
        except Exception as exc:
            log.exception("Дайджест за %s не собрался", day)
            await message.answer(f"Не получилось: {esc_html(str(exc))}", parse_mode="HTML")
            continue
        await send_long(message, result.html)


@router.message(Command("week"))
async def on_week(message: types.Message) -> None:
    """Сводка за 7 дней поверх дневных дайджестов (TZ §4.5)."""
    chats = await _digest_chats()
    if not chats:
        await message.answer("Ни для одной группы дайджест не включён.")
        return

    for chat in chats:
        digests = await repo.list_digests(chat.id, days=7, until=_today())
        if not digests:
            await message.answer(f"За неделю нет ни одного дайджеста «{chat.title}».")
            continue

        titles: list[str] = []
        for digest in digests:
            for topic in digest.topics:
                line = topic.get("title", "")
                if topic.get("decision"):
                    line += f" — {topic['decision']}"
                titles.append(f"{digest.day:%d.%m}: {line}")

        if not titles:
            await message.answer("За неделю тем не набралось.")
            continue

        try:
            data, _ = await chat_json(
                [
                    {"role": "system", "content": prompts.REDUCE_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.REDUCE_USER.format(topics="\n".join(titles[:60])),
                    },
                ],
                purpose="summary",
                chat_id=chat.id,
            )
            highlights = [str(h) for h in (data.get("highlights") or [])][:3]
        except Exception as exc:
            log.exception("Сводка за неделю не собралась")
            await message.answer(f"Не получилось: {esc_html(str(exc))}", parse_mode="HTML")
            continue

        lines = [f"<b>Неделя «{esc_html(chat.title or '')}»</b>", ""]
        lines += [f"• {esc_html(h)}" for h in highlights]
        lines += ["", f"<i>дайджестов за период: {len(digests)}, тем: {len(titles)}</i>"]
        await send_long(message, "\n".join(lines))


@router.message(Command("search"))
async def on_search(message: types.Message, command: CommandObject) -> None:
    """10 сообщений с дип-линками, без обращения к модели (TZ §4.5)."""
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напиши так: /search цена подписки")
        return

    found = await repo.search_messages(query, limit=10)
    if not found:
        await message.answer("Ничего не нашлось.")
        return

    chats = {c.id: c for c in await repo.list_chats()}
    lines = [f"<b>Нашлось по запросу «{esc_html(query)}»</b>", ""]
    for msg in found:
        chat = chats.get(msg.chat_id)
        snippet = (msg.content or "")[:160].replace("\n", " ")
        link = deeplink(chat.tg_id, msg.tg_msg_id) if chat else ""
        author = esc_html(msg.author_name or "аноним")
        lines.append(
            f'{fmt_local(msg.date)} <b>{author}</b>: {esc_html(snippet)} '
            f'<a href="{link}">↗️</a>'
        )
    await send_long(message, "\n".join(lines))


@router.message(Command("topics"))
async def on_topics(message: types.Message) -> None:
    """Темы за 30 дней — по сохранённым дайджестам (TZ §4.5)."""
    chats = await _digest_chats()
    lines: list[str] = ["<b>Темы за 30 дней</b>", ""]
    total = 0
    for chat in chats:
        digests = await repo.list_digests(chat.id, days=30, until=_today())
        for digest in digests:
            for topic in digest.topics[:3]:
                total += 1
                lines.append(
                    f"{digest.day:%d.%m} · {esc_html(str(topic.get('title', '')))}"
                )
    if not total:
        await message.answer("Дайджестов за 30 дней пока нет.")
        return
    await send_long(message, "\n".join(lines))


@router.message(Command("who"))
async def on_who(message: types.Message, command: CommandObject) -> None:
    """Чем занят участник (TZ §4.5). Места в рейтингах добавятся на шаге 12.5."""
    name = (command.args or "").strip()
    if not name:
        await message.answer("Напиши так: /who Вася")
        return

    found = await repo.get_author_activity(name, days=30)
    if not found:
        await message.answer("Такого участника не нашёл за последние 30 дней.")
        return

    lines = [
        f"<b>{esc_html(str(found['name'] or name))}</b>",
        f"Сообщений за 30 дней: {found['messages']}",
        f"Из них ответов: {found['replies']}, голосовых: {found['voices']}",
    ]
    if found.get("last_at"):
        lines.append(
            "Последнее сообщение: " + fmt_local(found["last_at"], "%d.%m.%Y %H:%M")
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("pin"))
async def on_pin(message: types.Message) -> None:
    """Пометить сообщение важным — приоритет в дайджесте (TZ §4.5, §4.7)."""
    target = message.reply_to_message
    if target is None:
        await message.answer("Ответь этой командой на сообщение, которое надо отметить.")
        return

    chat = await repo.get_chat_by_tg_id(target.chat.id)
    if chat is None:
        await message.answer("Это сообщение не из отслеживаемой группы.")
        return

    ok = await repo.set_pinned(chat.id, target.message_id)
    await message.answer(
        "Отметил, тема получит приоритет в дайджесте." if ok else
        "Сообщения нет в базе — возможно, коллектор его ещё не записал."
    )


@router.message(Command("stats"))
async def on_stats(message: types.Message) -> None:
    """Что собрано и сколько потрачено (TZ §4.5)."""
    stats = await repo.get_collection_stats()
    chats = await repo.list_chats()
    last = stats["last_at"]

    lines = [
        "<b>Что собрано</b>",
        f"Сообщений: {stats['messages']}",
        f"Участников: {stats['authors']}",
        f"Голосовых: {stats['voices']}, из них расшифровано: {stats['transcribed']}",
        f"Последнее сообщение: {fmt_local(last, '%d.%m.%Y %H:%M')}"
        if last else "Сообщений ещё нет",
        f"Дайджестов сохранено: {stats['digests']}",
        "",
        "<b>Расход модели за 30 дней</b>",
        f"Вызовов: {stats['llm_calls']}",
        f"Токенов на вход: {stats['tokens_in']}, на выход: {stats['tokens_out']}",
        "",
        "<b>Группы</b>",
    ]
    for chat in chats:
        flags = []
        if chat.collect:
            flags.append("сбор")
        if chat.digest:
            flags.append("дайджест")
        lines.append(
            f"• {esc_html(chat.title or str(chat.tg_id))} — "
            + (", ".join(flags) if flags else "выключена")
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


def _today() -> date_type:
    return datetime.now(ZoneInfo(cfg.TZ)).date()
