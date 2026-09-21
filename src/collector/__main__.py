"""Запуск коллектора: `make run-collector` или `python -m src.collector --dry-run`."""
from __future__ import annotations

import argparse
import asyncio
import logging

from src.collector.service import run
from src.config import cfg
from src.db import pool


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Collector: сбор сообщений закрытой группы")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="печатать сообщения в консоль, ничего не писать в БД",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=cfg.LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    try:
        await run(dry_run=args.dry_run)
    finally:
        await pool.close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nОстановлено")
