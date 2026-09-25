"""Создание aiogram-бота в одном месте: для процесса бота и для админки.

BOT_API_URL позволяет направить бота на подставной Bot API — так всё, что
бот «отправляет», можно проверить локально, не трогая живой Telegram.
"""
from __future__ import annotations

from typing import Any

from aiogram import Bot

from src.config import cfg


def create_bot(**kw: Any) -> Bot:
    if cfg.BOT_API_URL:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        kw.setdefault(
            "session",
            AiohttpSession(api=TelegramAPIServer.from_base(cfg.BOT_API_URL.rstrip("/"))),
        )
    return Bot(cfg.BOT_TOKEN, **kw)
