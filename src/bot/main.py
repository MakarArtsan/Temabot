"""Точка входа бота (aiogram 3): роутеры, мидлвари, расписание (TZ §4.5-4.6)."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from src.bot import handlers_copier as copier
from src.bot import handlers_qa
from src.bot.middlewares import OwnerOnly, RateLimit
from src.config import cfg
from src.db import pool
from src.db.migrate import apply_schema
from src.digest.scheduler import build_scheduler

log = logging.getLogger(__name__)


_dispatcher: Dispatcher | None = None


def build_dispatcher() -> Dispatcher:
    """Собрать диспетчер. На процесс он один и собирается один раз.

    Порядок важен (TZ §4.6): роутер копировщика идёт первым и публичен — он
    работает для всех участников разрешённых групп. Роутер Q&A идёт вторым и
    закрыт мидлварью на владельца; его перехватчик «любой текст — это вопрос»
    сработает только в личке и только если копировщик сообщение не взял.

    Результат кэшируется: роутеры — модульные синглтоны, и повторная сборка
    навесила бы мидлвари второй раз, а aiogram запретил бы привязать один и тот
    же роутер к двум диспетчерам.
    """
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher

    qa = handlers_qa.router
    qa.message.middleware(OwnerOnly())
    qa.message.middleware(RateLimit())
    qa.callback_query.middleware(OwnerOnly())

    dp = Dispatcher()
    dp.include_router(copier.router)
    dp.include_router(qa)
    _dispatcher = dp
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

    # кэшируем username и поднимаем Telegraph до начала приёма сообщений
    await copier.init_copier(bot)

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
