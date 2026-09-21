"""Применение схемы: `make migrate` и автоматически при старте бота (TZ шаг 13).

schema.sql идемпотентный, поэтому повторный запуск безопасен.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from src.config import cfg
from src.db import pool

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def apply_schema(dsn: str | None = None) -> None:
    sql = SCHEMA_PATH.read_text("utf-8")
    db = await pool.get_pool(dsn)
    async with db.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(
            """
            insert into state (key, value) values ('schema_applied_at', $1::jsonb)
            on conflict (key) do update set value = excluded.value
            """,
            f'"{datetime.now(UTC).isoformat()}"',
        )
    log.info("Схема применена: %s", SCHEMA_PATH)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Применить src/db/schema.sql")
    parser.add_argument("--dsn", default=cfg.DATABASE_URL or None)
    args = parser.parse_args()
    if not args.dsn:
        raise SystemExit("Не задан DATABASE_URL (см. .env.example)")
    try:
        await apply_schema(args.dsn)
        print("OK: схема применена")
    finally:
        await pool.close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=cfg.LOG_LEVEL)
    asyncio.run(_main())
