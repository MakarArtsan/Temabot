"""TelegramClient (Telethon): сессия, опциональный SOCKS5-прокси, retry (TZ §4.1).

Сессия берётся из TG_SESSION_STRING (деплой, TZ шаг 13) либо из файла TG_SESSION
(локальный запуск). Файл сессии — это полный доступ к аккаунту: права 600,
в git не коммитится.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from src.config import cfg

log = logging.getLogger(__name__)

T = TypeVar("T")

# Пауза после FloodWait не может расти бесконечно: если Telegram просит ждать
# больше суток, это не временный лимит, а повод остановиться и разобраться.
MAX_FLOOD_WAIT_SEC = 3600


def parse_proxy(url: str | None = None) -> dict[str, Any] | None:
    """PROXY_URL вида socks5://user:pass@host:port -> словарь для python-socks."""
    raw = (url if url is not None else cfg.PROXY_URL).strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"PROXY_URL разобран не полностью: {raw!r}")
    proxy: dict[str, Any] = {
        "proxy_type": parsed.scheme or "socks5",
        "addr": parsed.hostname,
        "port": parsed.port,
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def build_client() -> TelegramClient:
    """Собрать клиента. Приоритет у StringSession — так работает деплой."""
    if not cfg.TG_API_ID or not cfg.TG_API_HASH:
        raise SystemExit("Не заданы TG_API_ID / TG_API_HASH — см. docs/SETUP.md")

    session: StringSession | str
    if cfg.TG_SESSION_STRING:
        session = StringSession(cfg.TG_SESSION_STRING)
        log.info("Сессия: TG_SESSION_STRING")
    else:
        path = Path(cfg.TG_SESSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        session = str(path)
        log.info("Сессия: файл %s", path)

    return TelegramClient(
        session,
        cfg.TG_API_ID,
        cfg.TG_API_HASH,
        proxy=parse_proxy(),
        # аккаунт один, вторая копия процесса = AUTH_KEY_DUPLICATED (TZ шаг 13)
        connection_retries=5,
        retry_delay=2,
        auto_reconnect=True,
    )


def harden_session_file(path: str | Path | None = None) -> None:
    """Права 600 на .session — в ТЗ это отдельным пунктом (§0)."""
    target = Path(path or cfg.TG_SESSION)
    if target.exists():
        target.chmod(0o600)


async def with_flood_retry(
    action: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_delay: float = 2.0,
    description: str = "запрос к Telegram",
) -> T:
    """Выполнить запрос, переживая FloodWaitError и сетевые сбои.

    FloodWait — ждём ровно столько, сколько просит Telegram (плюс джиттер).
    Сетевые ошибки — экспоненциальная пауза 2, 4, 8, 16 секунд.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await action()
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 0))
            if wait > MAX_FLOOD_WAIT_SEC:
                log.error("FloodWait %s сек — это слишком долго, %s прерван", wait, description)
                raise
            delay = wait + random.uniform(0.5, 2.0)
            log.warning("FloodWait %s сек на «%s», ждём %.1f", wait, description, delay)
            await asyncio.sleep(delay)
            last_error = exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            delay = base_delay * (2**attempt)
            log.warning(
                "Сетевая ошибка на «%s» (%s), попытка %s/%s, пауза %.0f сек",
                description, exc, attempt + 1, attempts, delay,
            )
            await asyncio.sleep(delay)
            last_error = exc
    assert last_error is not None
    raise last_error
