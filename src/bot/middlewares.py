"""Мидлвари доступа (TZ §4.5, §4.8, §9).

Главное правило: в личке бот разговаривает только с владельцем. Групповые
разрешения появляются на шаге 10 вместе с роутером копировщика.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.config import cfg
from src.db import repo
from src.db.models import Chat

log = logging.getLogger(__name__)


class OwnerOnly(BaseMiddleware):
    """Пропускает только владельца. Чужим не отвечает вовсе.

    Молчание намеренное: содержимое закрытой группы не должно утекать никому,
    а вежливый отказ подтвердил бы, что бот вообще что-то знает (TZ §9).
    """

    def __init__(self, owner_id: int | None = None) -> None:
        self.owner_id = owner_id if owner_id is not None else cfg.OWNER_ID

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.owner_id:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступно только владельцу", show_alert=True)
            else:
                log.info("Игнорирую сообщение от %s", getattr(user, "id", "?"))
            return None
        return await handler(event, data)


class RateLimit(BaseMiddleware):
    """Не больше N действий в минуту на пользователя (TZ §4.8)."""

    def __init__(self, limit: int = 20, window_sec: float = 60.0) -> None:
        self.limit = limit
        self.window = window_sec
        self._hits: dict[int, list[float]] = defaultdict(list)

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        hits = [t for t in self._hits[user_id] if now - t < self.window]
        self._hits[user_id] = hits
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and not self.allow(user.id):
            log.warning("Превышен лимит запросов у %s", user.id)
            if isinstance(event, Message):
                await event.answer("Слишком часто. Подожди минуту.")
            return None
        return await handler(event, data)


class SettingsCache:
    """Настройки групп и блок-лист, перечитываемые раз в минуту (TZ §4.9).

    Без кэша каждое сообщение в группе стоило бы двух запросов в БД. Минута —
    это и есть обещанное ТЗ «перезапуск не нужен»: переключил флаг в админке,
    через минуту бот уже знает.
    """

    def __init__(self, ttl_sec: float = 60.0) -> None:
        self.ttl = ttl_sec
        self._chats: dict[int, Chat | None] = {}
        self._chats_at: dict[int, float] = {}
        self._blocked: set[int] = set()
        self._blocked_at = 0.0

    async def chat(self, chat_tg_id: int, *, now: float | None = None) -> Chat | None:
        now = now if now is not None else time.monotonic()
        seen = self._chats_at.get(chat_tg_id)
        if seen is not None and now - seen < self.ttl:
            return self._chats[chat_tg_id]
        found = await repo.get_chat_by_tg_id(chat_tg_id)
        self._chats[chat_tg_id] = found
        self._chats_at[chat_tg_id] = now
        return found

    async def blocked(self, *, now: float | None = None) -> set[int]:
        now = now if now is not None else time.monotonic()
        if now - self._blocked_at >= self.ttl:
            self._blocked = await repo.blocked_user_ids()
            self._blocked_at = now
        return self._blocked

    def forget(self, chat_tg_id: int | None = None) -> None:
        """Сбросить кэш сразу после изменения настроек из бота."""
        if chat_tg_id is None:
            self._chats.clear()
            self._chats_at.clear()
            self._blocked_at = 0.0
        else:
            self._chats.pop(chat_tg_id, None)
            self._chats_at.pop(chat_tg_id, None)


settings_cache = SettingsCache()


class CopierAccess(BaseMiddleware):
    """Кто и где может пользоваться копировщиком (TZ §4.8).

    В группе — только если `copier = allow`. Пока владелец не решил (`ask`),
    бот молчит: он не должен работать в чате, куда его добавили без спроса.
    В личке — только владелец, иначе любой желающий получил бы себе бесплатный
    Telegraph-постер от имени бота.
    """

    def __init__(self, cache: SettingsCache | None = None, owner_id: int | None = None) -> None:
        self.cache = cache or settings_cache
        self.owner_id = owner_id if owner_id is not None else cfg.OWNER_ID

    async def allowed(self, chat_id: int, chat_type: str, user_id: int | None) -> bool:
        if user_id is not None and user_id in await self.cache.blocked():
            return False
        if chat_type == "private":
            return user_id == self.owner_id
        chat = await self.cache.chat(chat_id)
        return bool(chat and chat.copier == "allow")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = event if isinstance(event, Message) else None
        if message is None:
            return await handler(event, data)

        user = data.get("event_from_user")
        if not await self.allowed(
            message.chat.id, message.chat.type, user.id if user else None
        ):
            log.debug("Копировщик молчит в чате %s", message.chat.id)
            return None
        return await handler(event, data)


class CopierRateLimit(RateLimit):
    """Не больше 10 копирований в минуту на пользователя (TZ §4.8)."""

    def __init__(self) -> None:
        super().__init__(limit=10, window_sec=60.0)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and not self.allow(user.id):
            # в группе молчим: предупреждение о лимите — это тоже флуд
            log.info("Лимит копирований исчерпан у %s", user.id)
            return None
        return await handler(event, data)
