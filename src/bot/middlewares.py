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
