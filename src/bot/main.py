"""Точка входа бота (aiogram 3): роутеры, мидлвари, расписание (TZ §4.5-4.6)."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties

from src.bot import (
    handlers_admin,
    handlers_feedback,
    handlers_publish,
    handlers_qa,
    handlers_ratings,
)
from src.bot import handlers_copier as copier
from src.bot.client import create_bot
from src.bot.middlewares import (
    CopierAccess,
    CopierRateLimit,
    LastUpdateMark,
    OwnerOnly,
    RateLimit,
)
from src.config import cfg
from src.db import pool, repo
from src.db.migrate import apply_schema
from src.digest.scheduler import build_scheduler

log = logging.getLogger(__name__)


_dispatcher: Dispatcher | None = None


def build_dispatcher() -> Dispatcher:
    """Собрать диспетчер. На процесс он один и собирается один раз.

    Порядок важен (TZ §4.6, §4.8): копировщик идёт первым и публичен, но его
    мидлвари пускают только разрешённые группы и не пускают забаненных. Дальше
    админ-роутер и Q&A — оба только для владельца. Перехватчик «любой текст —
    это вопрос» стоит последним и срабатывает лишь в личке, если сообщение не
    взяли ни копировщик, ни команды.

    Результат кэшируется: роутеры — модульные синглтоны, и повторная сборка
    навесила бы мидлвари второй раз, а aiogram запретил бы привязать один и тот
    же роутер к двум диспетчерам.
    """
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher

    # копировщик публичный, но только в разрешённых группах и не для забаненных
    copier.router.message.middleware(CopierAccess())
    copier.router.message.middleware(CopierRateLimit())

    admin = handlers_admin.router
    admin.message.middleware(OwnerOnly())
    admin.callback_query.middleware(OwnerOnly())

    feedback = handlers_feedback.router
    feedback.callback_query.middleware(OwnerOnly())

    # публикация дайджеста в группу — кнопки только у владельца
    publish = handlers_publish.router
    publish.callback_query.middleware(OwnerOnly())

    # /optout доступен участникам группы, поэтому мидлвари владельца здесь нет
    ratings = handlers_ratings.router

    qa = handlers_qa.router
    qa.message.middleware(OwnerOnly())
    qa.message.middleware(RateLimit())
    qa.callback_query.middleware(OwnerOnly())

    dp = Dispatcher()
    dp.update.outer_middleware(LastUpdateMark())
    dp.include_router(copier.router)
    dp.include_router(admin)
    dp.include_router(feedback)
    dp.include_router(publish)
    dp.include_router(ratings)
    dp.include_router(qa)
    _dispatcher = dp
    return dp


WEBHOOK_KEY = "bot:webhook"


async def check_updates_channel(bot: Any) -> None:
    """Снять вебхук, если он остался от прежнего хостинга бота.

    Пока у бота есть вебхук, Telegram отдаёт входящие туда, а getUpdates
    отвечает конфликтом: дайджесты уходят (это исходящие), а на упоминания и
    команды бот молчит. Сообщения, накопившиеся в очереди, не выбрасываем.
    """
    try:
        info = await bot.get_webhook_info()
    except Exception:
        log.warning("Не удалось проверить вебхук бота", exc_info=True)
        return
    url = getattr(info, "url", "") or ""
    if not url:
        return
    host = urlsplit(url).hostname or "?"
    log.warning(
        "У бота был вебхук на %s — снимаю, иначе входящие сюда не доходят "
        "(последняя ошибка вебхука: %s)", host, getattr(info, "last_error_message", None),
    )
    await bot.delete_webhook(drop_pending_updates=False)
    if cfg.DATABASE_URL:
        await repo.set_state(
            WEBHOOK_KEY, {"removed": host, "at": datetime.now(UTC).isoformat()}
        )


async def run() -> None:
    if not cfg.BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN — см. docs/SETUP.md")
    if not cfg.OWNER_ID:
        raise SystemExit("Не задан OWNER_ID: без него бот отвечал бы кому попало")

    if cfg.DATABASE_URL:
        if cfg.MIGRATE_ON_START:
            await apply_schema(cfg.DATABASE_URL)
        if cfg.TG_GROUP_ID:
            # копировщик в основной группе должен работать сразу после деплоя
            await repo.bootstrap_primary_chat(cfg.TG_GROUP_ID)

    bot = create_bot(default=DefaultBotProperties(parse_mode="HTML"))
    dp = build_dispatcher()

    me = await bot.get_me()
    log.info("Бот @%s запущен, владелец %s", me.username, cfg.OWNER_ID)

    if cfg.DATABASE_URL:
        try:
            # названия групп вместо номеров — в админке, дайджесте и /groups
            await asyncio.wait_for(handlers_admin.refresh_chat_titles(bot), 30)
        except Exception:
            log.warning("Названия групп не обновились, повторю ночью", exc_info=True)

    # входящие должны доходить до этого процесса — иначе копировщик и команды молчат
    await check_updates_channel(bot)

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
