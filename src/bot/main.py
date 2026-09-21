"""Точка входа бота (aiogram 3): роутеры, мидлвари, расписание (TZ §4.5-4.6)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from src.bot import handlers_qa
from src.bot.middlewares import OwnerOnly, RateLimit
from src.config import cfg
from src.db import pool
from src.db.migrate import apply_schema
from src.digest.scheduler import build_scheduler

log = logging.getLogger(__name__)


def build_dispatcher() -> Dispatcher:
    """Собрать диспетчер. Порядок роутеров важен: copier -> qa (TZ §4.6)."""
    dp = Dispatcher()

    # Роутер копировщика подключается на шаге 9 первым, до qa.
    qa = handlers_qa.router
    qa.message.middleware(OwnerOnly())
    qa.message.middleware(RateLimit())
    qa.callback_query.middleware(OwnerOnly())
    dp.include_router(qa)
    return dp


async def run() -> None:
    if not cfg.BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN — см. docs/SETUP.md")
    if not cfg.OWNER_ID:
        raise SystemExit("Не задан OWNER_ID: без него бот отвечал бы кому попало")

    if cfg.DATABASE_URL:
        await apply_schema(cfg.DATABASE_URL)

    bot = Bot(cfg.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = build_dispatcher()

    me = await bot.get_me()
    log.info("Бот @%s запущен, владелец %s", me.username, cfg.OWNER_ID)

    scheduler = build_scheduler(bot)
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await pool.close_pool()


def main() -> None:
    logging.basicConfig(
        level=cfg.LOG_LEVEL, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    logging.getLogger("aiogram").setLevel(logging.INFO)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
