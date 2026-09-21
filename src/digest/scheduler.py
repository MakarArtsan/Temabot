"""Расписание: дайджест в 23:30 по Asia/Kamchatka (TZ §4.3, §4.5).

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
from src.digest.render import split_message
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
            for part in split_message(result.html):
                await bot.send_message(
                    cfg.OWNER_ID, part, parse_mode="HTML",
                    link_preview_options={"is_disabled": True},
                )
            sent += 1
        except Exception:
            log.exception("Не удалось отправить дайджест группы %s", chat.tg_id)

    await repo.set_state(
        "digest:last_run",
        {"day": day.isoformat(), "sent": sent, "at": datetime.now(ZoneInfo(cfg.TZ)).isoformat()},
    )
    log.info("Дайджестов отправлено: %s из %s", sent, len(chats))
    return sent


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
    scheduler.add_job(
        heartbeat,
        CronTrigger(minute="*", timezone=zone),
        args=[bot],
        id="heartbeat",
        replace_existing=True,
    )
    log.info("Дайджест запланирован на %s по %s", moment.strftime("%H:%M"), cfg.TZ)
    return scheduler
