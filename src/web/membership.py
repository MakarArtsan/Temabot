"""Кого пускать на страницу участников (решение владельца, TZ §9).

Только тех, кто сейчас состоит в группе. Порядок проверки:

1. Bot API `getChatMember` — живой ответ Telegram. «Состоит» — пускаем,
   «удалён из группы» (kicked) — не пускаем.
2. Если Telegram ответить не смог или сказал «не состоит» (обычному боту
   Telegram гарантирует ответ, только если бот — администратор, а при скрытом
   списке участников бот видит не всех), решает список коллектора — но только
   свежий, сверенный не позже суток назад.
3. Ничего не известно — не пускаем.

Ответ запоминается ненадолго: страницы открываются быстро, а вышедший из
группы теряет доступ в пределах нескольких минут.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from src.config import cfg
from src.db import repo
from src.db.models import Chat

log = logging.getLogger(__name__)

TELEGRAM_TIMEOUT_SEC = 10.0
ALLOW_TTL_SEC = 600.0     # «состоит» помним 10 минут
DENY_TTL_SEC = 60.0       # отказ — минуту: вдруг человек только что вступил

MEMBER_STATUSES = {"creator", "administrator", "member"}


@dataclass(slots=True)
class _Cached:
    allowed: bool
    until: float


_cache: dict[tuple[int, int], _Cached] = {}


def forget(chat_id: int | None = None) -> None:
    """Сбросить запомненные ответы — например, когда владелец поменял режим."""
    if chat_id is None:
        _cache.clear()
        return
    for key in [k for k in _cache if k[0] == chat_id]:
        _cache.pop(key, None)


def make_bot() -> Any:
    from src.bot.client import create_bot

    return create_bot()


async def telegram_status(chat_tg_id: int, user_id: int) -> str | None:
    """member | left | kicked — или None, если Telegram ответить не смог.

    Только чтение: в группу и человеку ничего не отправляется.
    """
    if not cfg.BOT_TOKEN:
        return None
    try:
        bot = make_bot()
    except Exception:
        log.warning("Бот для проверки участника не создался", exc_info=True)
        return None
    try:
        member = await asyncio.wait_for(
            bot.get_chat_member(chat_id=chat_tg_id, user_id=user_id), TELEGRAM_TIMEOUT_SEC
        )
    except Exception as exc:
        log.info("Telegram не ответил, состоит ли %s в %s: %s", user_id, chat_tg_id, exc)
        return None
    finally:
        await bot.session.close()

    status = str(getattr(member, "status", "") or "")
    if status in MEMBER_STATUSES:
        return "member"
    if status == "restricted":
        # ограниченный участник остаётся в группе, пока is_member = true
        return "member" if getattr(member, "is_member", False) else "left"
    if status == "kicked":
        return "kicked"
    if status == "left":
        return "left"
    return None


async def is_member(chat: Chat, user_id: int, *, now: float | None = None) -> bool:
    """Состоит ли человек в группе прямо сейчас (с коротким кэшем)."""
    if not user_id:
        return False
    if user_id == cfg.OWNER_ID:
        return True

    moment = time.monotonic() if now is None else now
    key = (chat.id, user_id)
    cached = _cache.get(key)
    if cached is not None and cached.until > moment:
        return cached.allowed

    status = await telegram_status(chat.tg_id, user_id)
    if status == "member":
        allowed = True
    elif status == "kicked":
        allowed = False
    else:
        allowed = await repo.chat_member_status(chat.id, user_id) is True

    ttl = ALLOW_TTL_SEC if allowed else DENY_TTL_SEC
    _cache[key] = _Cached(allowed, moment + ttl)
    if not allowed:
        log.info("Участник %s не прошёл проверку для группы %s", user_id, chat.tg_id)
    return allowed


async def visible_chats(user_id: int) -> list[Chat]:
    """Группы, чью страницу участников этот человек может открыть.

    Владелец видит все группы — чтобы посмотреть, как это выглядит, до того
    как открыть страницу участникам.
    """
    if user_id and user_id == cfg.OWNER_ID:
        return await repo.list_chats()
    chats = await repo.list_portal_chats()
    return [chat for chat in chats if await is_member(chat, user_id)]
