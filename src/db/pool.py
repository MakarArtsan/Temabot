"""Пул соединений asyncpg. SQL здесь не пишется — он живёт в repo.py."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

import asyncpg

from src.config import cfg

CONNECT_TIMEOUT_SEC = 15

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


async def _init_connection(conn: asyncpg.Connection) -> None:
    """jsonb ходит в код как dict, а не как строка."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


def dsn_problem(dsn: str) -> str | None:
    """Что не так со строкой подключения — без самой строки: в ней пароль.

    Самая частая беда — спецсимволы в пароле. `/`, `#`, `?` и `@` ломают разбор
    адреса, и asyncpg падает с ошибкой, в текст которой попадает кусок пароля.
    """
    if not dsn:
        return "Не задан DATABASE_URL"
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        return "DATABASE_URL должен начинаться с postgresql://"
    try:
        parts.port  # noqa: B018 — сам разбор порта и есть проверка
    except ValueError:
        return (
            "DATABASE_URL не разбирается: в пароле, скорее всего, есть символы "
            "/ # ? @ или :. Смените пароль базы на буквы и цифры "
            "(Supabase → Settings → Database → Reset password) и обновите DATABASE_URL"
        )
    if not parts.hostname:
        return "В DATABASE_URL нет адреса сервера базы"
    if dsn.count("@") > 1:
        return "В пароле DATABASE_URL есть символ @ — смените пароль базы на буквы и цифры"
    return None


def dsn_hint(dsn: str) -> str | None:
    """Подсказка, которая не мешает запуску, но объясняет частую ошибку.

    Прямое подключение Supabase (db.<проект>.supabase.co) доступно только по IPv6.
    На хостингах без IPv6 оно падает с «Network is unreachable».
    """
    host = (urlsplit(dsn).hostname or "") if dsn else ""
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return (
            "DATABASE_URL указывает на прямое подключение Supabase (db.….supabase.co) — "
            "оно работает только по IPv6. Возьми строку Session pooler: Supabase → Connect → "
            "Session pooler (хост …pooler.supabase.com, порт 5432)"
        )
    return None


def uses_transaction_pooler(dsn: str) -> bool:
    """Похоже ли, что подключение идёт через пулер в режиме транзакций.

    Supabase отдаёт такой пулер на порту 6543. Он переиспользует соединения
    между транзакциями, поэтому подготовленные выражения asyncpg ломаются:
    выражение, созданное в одном соединении, в другом уже неизвестно.
    Лечится отключением кэша выражений.
    """
    lowered = (dsn or "").lower()
    return ":6543" in lowered or "pgbouncer=true" in lowered


async def get_pool(dsn: str | None = None) -> asyncpg.Pool:
    """Ленивая инициализация общего пула. Потокобезопасна в рамках event loop."""
    global _pool
    if _pool is not None:
        return _pool

    target = dsn or cfg.DATABASE_URL
    problem = dsn_problem(target)
    if problem:
        # from None: иначе в трейсбек попадёт разбор строки вместе с паролем
        raise RuntimeError(problem) from None
    extra: dict[str, Any] = {}
    if cfg.DB_DISABLE_STATEMENT_CACHE or uses_transaction_pooler(target):
        # без этого через пулер-транзакций сыплется DuplicatePreparedStatementError
        extra["statement_cache_size"] = 0
        extra["max_cached_statement_lifetime"] = 0

    async with _lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                target,
                min_size=1,
                max_size=cfg.DB_POOL_SIZE,
                init=_init_connection,
                command_timeout=60,
                # по умолчанию asyncpg ждёт подключения минуту — слишком долго,
                # чтобы понять, что база недоступна
                timeout=CONNECT_TIMEOUT_SEC,
                **extra,
            )
    assert _pool is not None
    return _pool


async def set_pool(pool: asyncpg.Pool | None) -> None:
    """Подменить пул (тесты, отдельные скрипты)."""
    global _pool
    _pool = pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    pool = await get_pool()
    return await pool.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    pool = await get_pool()
    return await pool.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    pool = await get_pool()
    return await pool.execute(query, *args)
