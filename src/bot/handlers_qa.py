"""Команды владельца в личке (TZ §4.5).

`/ask` и ответ на произвольный текст появятся на шаге 8 вместе с RAG.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject

from src.bot.handlers_feedback import feedback_keyboard
from src.bot.handlers_publish import offer_publication
from src.config import cfg
from src.db import repo
from src.db.models import Chat
from src.digest import pipeline as digest_pipeline
from src.digest import prompts
from src.digest.render import DigestData, deeplink, esc_html, split_message
from src.digest.scheduler import send_digest
from src.llm.client import chat_json
from src.rag.answer import answer_question

log = logging.getLogger(__name__)
router = Router(name="qa")

HELP = """\
<b>Что я умею</b>

/ask вопрос — ответ по истории чата со ссылками на источники
(можно просто написать вопрос без команды)
/digest — дайджест за сегодня
/digest 2026-09-15 — за конкретный день
/missed — темы, не прошедшие порог
/week — сводка за 7 дней
/search запрос — 10 сообщений со ссылками, без обращения к модели
/topics — темы за 30 дней
/who имя — чем занят участник и его места в рейтингах
/top [день|неделя|месяц] [#группа] [номинация] — рейтинги участников
/pin — ответом на сообщение: пометить важным
/stats — что собрано и сколько потрачено

<b>В группе</b>
Упомяни меня с текстом — пришлю ссылку, откуда его удобно скопировать.
Ответь на чужое сообщение одним упоминанием — скопирую то сообщение.
Под скопированным из отслеживаемой группы будет кнопка «Что обсуждали вокруг» —
она работает только для меня и отвечает в личку.\
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


@router.message(Command("ask"))
async def on_ask(message: types.Message, command: CommandObject) -> None:
    """Ответ по истории чата (TZ §4.4). С `#группа` — только по одной группе."""
    question = (command.args or "").strip()
    if not question:
        await message.answer(
            "Напиши так: <code>/ask что решили про Seedance</code>\n"
            "Или по одной группе: <code>/ask #рабочая когда конференция</code>",
            parse_mode="HTML",
        )
        return
    await _answer(message, question)


async def split_group_filter(question: str) -> tuple[str, Chat | None, str | None]:
    """Выделить `#группа` из вопроса (TZ §4.8).

    Возвращает (вопрос без метки, найденная группа, текст ошибки).
    """
    if not question.startswith("#"):
        return question, None, None

    label, _, rest = question.partition(" ")
    name = label[1:].strip()
    rest = rest.strip()
    if not name:
        return rest, None, None

    found = await repo.find_chats(name)
    if not found:
        return rest, None, f"Группы «{name}» не знаю. Посмотреть список — /groups"
    if len(found) > 1:
        titles = ", ".join(c.title or str(c.tg_id) for c in found[:5])
        return rest, None, f"Под «{name}» подходит несколько групп: {titles}"
    return rest, found[0], None


async def _answer(message: types.Message, question: str) -> None:
    question, group, error = await split_group_filter(question)
    if error:
        await message.answer(error)
        return
    if not question:
        await message.answer("А вопрос?")
        return

    if message.bot is not None:
        # ответ собирается несколько секунд, «печатает…» показывает, что бот жив
        await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = await answer_question(question, chat_id=group.id if group else None)
    except Exception as exc:
        log.exception("Ответ на вопрос не собрался")
        await message.answer(f"Не получилось: {esc_html(str(exc))}", parse_mode="HTML")
        return
    await send_long(message, answer.as_html())


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
            # готовый дайджест собираем заново из сохранённой структуры,
            # кнопки берут id тем из БД
            data = DigestData.from_dict(stored.payload).titled(chat.title)
            items = await repo.get_digest_items(stored.id or 0, shown=True)
            by_thread = {row["thread_id"]: row["id"] for row in items}
            for topic in data.topics:
                topic.item_id = by_thread.get(topic.thread_id)
            await send_digest(message.bot, message.chat.id, data, topics=data.topics)
            await offer_publication(message.bot, chat, stored)
            continue

        await message.answer(f"Собираю дайджест «{chat.title or chat.tg_id}» за {day}…")
        try:
            result = await digest_pipeline.run_for_chat(chat, day)
        except Exception as exc:
            log.exception("Дайджест за %s не собрался", day)
            await message.answer(f"Не получилось: {esc_html(str(exc))}", parse_mode="HTML")
            continue
        await send_digest(message.bot, message.chat.id, result.data, topics=result.topics)
        if result.digest_id:
            stored = await repo.get_digest_by_id(result.digest_id)
            await offer_publication(message.bot, chat, stored)


@router.message(Command("missed"))
async def on_missed(message: types.Message, command: CommandObject) -> None:
    """Темы, которые не прошли порог (TZ §4.7).

    Если там нашлось важное, 👍 на нём тоже учится — именно так порог и
    настраивается под вкус владельца.
    """
    raw = (command.args or "").strip()
    try:
        day = date_type.fromisoformat(raw) if raw else _today()
    except ValueError:
        await message.answer("Дату нужно писать как 2026-09-15")
        return

    chats = await _digest_chats()
    found = False
    for chat in chats:
        stored = await repo.get_digest(chat.id, day)
        if stored is None or stored.id is None:
            continue
        items = await repo.get_digest_items(stored.id, shown=False)
        if not items:
            continue
        found = True
        threshold = 0.0
        for item in items:
            threshold = (item["features"] or {}).get("threshold", 0.0)
            takeaway = (item["features"] or {}).get("takeaway", "")
            text = (
                f"<b>{esc_html(str(item['title'] or ''))}</b>\n"
                f"{esc_html(takeaway)}\n"
                f"<i>скор {item['score']:.2f} при пороге {threshold:.2f}</i>"
            )
            await message.answer(
                text, parse_mode="HTML", reply_markup=feedback_keyboard(item["id"])
            )

    if not found:
        await message.answer(f"За {day} отсеянных тем нет.")


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

    from src.bot.handlers_ratings import who_ranks

    lines += await who_ranks(name)
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


@router.message(F.chat.type == "private", F.text & ~F.text.startswith("/"))
async def on_plain_text(message: types.Message) -> None:
    """Просто текст в личке — то же, что /ask (TZ §4.5, §4.6).

    Фильтр по типу чата обязателен: без него бот отвечал бы на каждое сообщение
    в группе, где он состоит как копировщик.
    """
    await _answer(message, (message.text or "").strip())
