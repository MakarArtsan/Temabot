"""Расписание: дайджест в 23:30 по TZ из настроек (по умолчанию Europe/Moscow) (TZ §4.3, §4.5).

Время берётся из `chats.digest_time`, поэтому у каждой группы оно своё, а
значение из БД перечитывается — перезапускать процесс ради смены времени не надо.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import cfg
from src.db import repo
from src.digest import pipeline as digest_pipeline
from src.digest.render import DigestData, digest_parts, split_message
from src.jobs.ratings import recalc_all
from src.jobs.retrain import retrain_all
from src.rag.index import index_all

log = logging.getLogger(__name__)

DEFAULT_DIGEST_TIME = time(23, 30)
HEARTBEAT_KEY = "bot:heartbeat"


def local_today() -> date_type:
    return datetime.now(ZoneInfo(cfg.TZ)).date()


async def send_daily_digests(bot: Any, *, day: date_type | None = None) -> int:
    """Собрать и отправить дайджесты владельцу. Возвращает число отправленных."""
    day = day or local_today()
    chats = await repo.list_chats(digest=True)
    sent = 0

    for chat in chats:
        try:
            result = await digest_pipeline.run_for_chat(chat, day)
        except Exception:
            # одна сломанная группа не должна отменять дайджест остальных
            log.exception("Дайджест группы %s за %s не собрался", chat.tg_id, day)
            continue

        try:
            await send_digest(bot, cfg.OWNER_ID, result.data, topics=result.topics)
            sent += 1
        except Exception:
            log.exception("Не удалось отправить дайджест группы %s", chat.tg_id)

    await repo.set_state(
        "digest:last_run",
        {"day": day.isoformat(), "sent": sent, "at": datetime.now(ZoneInfo(cfg.TZ)).isoformat()},
    )
    log.info("Дайджестов отправлено: %s из %s", sent, len(chats))
    return sent


async def send_digest(
    bot: Any, chat_id: int, data: DigestData | None, *, topics: list[Any] | None = None
) -> int:
    """Отправить дайджест: шапка, темы с кнопками оценки, хвост (TZ §4.7).

    Возвращает число отправленных сообщений.
    """
    from src.bot.handlers_feedback import feedback_keyboard

    if data is None:
        return 0

    by_thread = {t.thread_id: t for t in (topics or [])}
    sent = 0
    for text, topic in digest_parts(data):
        markup = None
        if topic is not None:
            # id темы известен только после сохранения в БД
            stored = by_thread.get(topic.thread_id)
            item_id = getattr(stored, "item_id", None) or getattr(topic, "item_id", None)
            if item_id:
                markup = feedback_keyboard(item_id)
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            # кнопки вешаем на последний кусок: под ним они и видны
            await bot.send_message(
                chat_id, chunk, parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                reply_markup=markup if index == len(chunks) - 1 else None,
            )
            sent += 1
    return sent


async def send_period_report(bot: Any, period_key: str) -> int:
    """Автоотчёт по рейтингам владельцу (TZ §4.10)."""
    from src.bot.handlers_ratings import resolve_period
    from src.jobs.nominations import NOMINATIONS, top_of, usefulness_scale

    period = resolve_period(period_key)
    sent = 0
    for chat in await repo.list_chats(digest=True):
        rows = await repo.get_author_stats(
            chat.id, date_from=period.date_from, date_to=period.date_to
        )
        if not rows:
            continue
        scale = usefulness_scale(rows)
        lines = [f"<b>Итоги · {period.title} · {chat.title or chat.tg_id}</b>", ""]
        for nomination in NOMINATIONS:
            top = top_of(nomination, rows, limit=3)
            if not top:
                continue
            lines.append(f"<b>{nomination.title}</b>")
            for place, (row, value) in enumerate(top):
                name = row.get("name") or row["tg_user_id"]
                shown = (
                    f"{scale.get(int(row['tg_user_id']), 0)} из 100"
                    if nomination.key == "useful"
                    else f"{value:g} {nomination.unit}".strip()
                )
                lines.append(f"{'🥇🥈🥉'[place]} {name} — {shown}")
            lines.append("")
        try:
            for chunk in split_message("\n".join(lines).strip()):
                await bot.send_message(cfg.OWNER_ID, chunk, parse_mode="HTML")
            sent += 1
        except Exception:
            log.exception("Автоотчёт по группе %s не ушёл", chat.tg_id)
    return sent


async def send_weekly_report(bot: Any) -> int:
    sent = await send_period_report(bot, "week")
    # и, если владелец это включил, публикация в саму группу (TZ §4.10)
    from src.bot.handlers_ratings import publish_ratings

    for chat in await repo.list_chats(digest=True):
        await publish_ratings(bot, chat, "week")
    return sent


async def send_monthly_report(bot: Any) -> int:
    return await send_period_report(bot, "month")


async def heartbeat(bot: Any) -> None:
    """Отметка живости для страницы «Система» в админке (TZ §4.9)."""
    await repo.set_state(
        HEARTBEAT_KEY, {"at": datetime.now(ZoneInfo(cfg.TZ)).isoformat()}
    )


def build_scheduler(bot: Any, *, digest_time: time | None = None) -> AsyncIOScheduler:
    zone = ZoneInfo(cfg.TZ)
    scheduler = AsyncIOScheduler(timezone=zone)
    moment = digest_time or DEFAULT_DIGEST_TIME

    scheduler.add_job(
        send_daily_digests,
        CronTrigger(hour=moment.hour, minute=moment.minute, timezone=zone),
        args=[bot],
        id="daily_digest",
        replace_existing=True,
        misfire_grace_time=3600,  # процесс мог перезапускаться — дайджест всё равно уйдёт
        coalesce=True,            # два пропущенных запуска не дадут два дайджеста
    )
    scheduler.add_job(
        index_all,
        CronTrigger(minute=17, timezone=zone),   # раз в час, не в ноль минут
        id="rag_index",
        replace_existing=True,
        coalesce=True,
        max_instances=1,        # индексация может идти дольше часа на большой истории
    )
    # рейтинги считаются после дайджеста: формуле полезности нужны его оценки
    scheduler.add_job(
        recalc_all,
        CronTrigger(hour=23, minute=40, timezone=zone),
        id="ratings",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        send_weekly_report,
        CronTrigger(day_of_week="sun", hour=23, minute=45, timezone=zone),
        args=[bot],
        id="weekly_report",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        send_monthly_report,
        CronTrigger(day=1, hour=0, minute=30, timezone=zone),
        args=[bot],
        id="monthly_report",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        retrain_all,
        CronTrigger(day_of_week="mon", hour=4, minute=0, timezone=zone),
        id="retrain",
        replace_existing=True,
        coalesce=True,
    )
    scheduler.add_job(
        heartbeat,
        CronTrigger(minute="*", timezone=zone),
        args=[bot],
        id="heartbeat",
        replace_existing=True,
    )
    log.info("Дайджест запланирован на %s по %s", moment.strftime("%H:%M"), cfg.TZ)
    return scheduler
