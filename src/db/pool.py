"""Пул соединений asyncpg. SQL здесь не пишется — он живёт в repo.py."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg

from src.config import cfg

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
