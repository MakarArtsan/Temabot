"""Одноразовая загрузка истории (TZ §4.1, шаг 4).

Порции по 200 сообщений с паузой 1.5 секунды — чтобы не ловить FloodWait на
чужом аккаунте. Прогресс печатается в консоль, точка остановки пишется в `state`,
поэтому прерванный бэкфилл продолжается с того же места, а не с начала.

История читается от свежих к старым: так первым в базе окажется то, что нужнее
всего для дайджеста, даже если процесс прервут на середине.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.collector.client import build_client, with_flood_retry
from src.collector.service import Collector, connect
from src.config import cfg
from src.db import pool, repo
from src.db.models import Chat

log = logging.getLogger(__name__)

BATCH_SIZE = 200
PAUSE_SEC = 1.5
DEFAULT_DAYS = 7


def state_key(chat_tg_id: int) -> str:
    return f"backfill:{chat_tg_id}"


@dataclass(slots=True)
class BackfillResult:
    chat_tg_id: int
    saved: int = 0
    scanned: int = 0
    batches: int = 0
    done: bool = False
    oldest_date: datetime | None = None

    def describe(self) -> str:
        edge = f"{self.oldest_date:%Y-%m-%d %H:%M}" if self.oldest_date else "—"
        status = "история пройдена" if self.done else "остановлено, можно продолжить"
        return (
            f"чат {self.chat_tg_id}: сохранено {self.saved} из {self.scanned} "
            f"просмотренных, порций {self.batches}, дошли до {edge} ({status})"
        )


def _is_service_message(message: Any) -> bool:
    """«Вася добавил Петю в группу» — в дайджесте и поиске это шум."""
    return bool(getattr(message, "action", None))


async def backfill_chat(
    collector: Collector,
    chat: Chat,
    *,
    days: int = DEFAULT_DAYS,
    batch_size: int = BATCH_SIZE,
    pause_sec: float = PAUSE_SEC,
    restart: bool = False,
    now: datetime | None = None,
) -> BackfillResult:
    """Загрузить историю чата за последние `days` дней."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    result = BackfillResult(chat_tg_id=chat.tg_id)

    offset_id = 0  # 0 = начать с самого свежего сообщения
    if not restart:
        saved_state = await repo.get_state(state_key(chat.tg_id)) or {}
        if saved_state.get("done"):
            log.info("Чат %s уже пройден. Нужен повтор — запусти с --restart", chat.tg_id)
            result.done = True
            return result
        offset_id = int(saved_state.get("offset_id") or 0)
        if offset_id:
            log.info("Продолжаю чат %s с сообщения #%s", chat.tg_id, offset_id)

    while True:
        batch: list[Any] = await with_flood_retry(
            functools.partial(
                collector.client.get_messages,
                chat.tg_id,
                limit=batch_size,
                offset_id=offset_id,
            ),
            description=f"история чата {chat.tg_id}",
        )
        if not batch:
            result.done = True
            break

        reached_cutoff = False
        for message in batch:
            result.scanned += 1
            date = getattr(message, "date", None)
            if date and date < cutoff:
                reached_cutoff = True
                break
            if _is_service_message(message):
                continue
            try:
                await collector.save(message, chat)
                result.saved += 1
                result.oldest_date = date
            except Exception:
                log.exception("Пропускаю сообщение %s", getattr(message, "id", "?"))

        offset_id = int(batch[-1].id)
        result.batches += 1
        await _remember(chat, offset_id, done=reached_cutoff, dry_run=collector.dry_run)

        edge = f"{result.oldest_date:%Y-%m-%d %H:%M}" if result.oldest_date else "—"
        print(
            f"  порция {result.batches}: сохранено всего {result.saved}, дошли до {edge}"
        )

        if reached_cutoff:
            result.done = True
            break
        if len(batch) < batch_size:
            # Telegram отдал меньше, чем просили — дальше истории нет
            result.done = True
            break
        await asyncio.sleep(pause_sec)

    await _remember(chat, offset_id, done=result.done, dry_run=collector.dry_run)
    return result


async def _remember(chat: Chat, offset_id: int, *, done: bool, dry_run: bool) -> None:
    if dry_run:
        return
    await repo.set_state(
        state_key(chat.tg_id),
        {"offset_id": offset_id, "done": done, "updated_at": datetime.now(UTC).isoformat()},
    )


async def run(
    *,
    days: int = DEFAULT_DAYS,
    chat_tg_id: int | None = None,
    dry_run: bool = False,
    restart: bool = False,
) -> list[BackfillResult]:
    client = build_client()
    collector = Collector(client, dry_run=dry_run)

    await connect(client)
    chats = await collector.load_target_chats()
    if chat_tg_id is not None:
        chats = [c for c in chats if c.tg_id == chat_tg_id]
        if not chats:
            raise SystemExit(f"Чат {chat_tg_id} не найден или у него collect = false")
    if not chats:
        raise SystemExit("Нет ни одной группы с collect = true")

    results: list[BackfillResult] = []
    for chat in chats:
        print(f"Бэкфилл {chat.title or chat.tg_id} за {days} дн.")
        result = await backfill_chat(collector, chat, days=days, restart=restart)
        print(f"Готово: {result.describe()}")
        results.append(result)

    await client.disconnect()
    return results


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка истории группы порциями")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="за сколько дней")
    parser.add_argument("--chat", type=int, default=None, help="tg_id конкретной группы")
    parser.add_argument("--dry-run", action="store_true", help="печатать, не писать в БД")
    parser.add_argument(
        "--restart", action="store_true", help="начать заново, игнорируя сохранённую точку"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=cfg.LOG_LEVEL, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    try:
        await run(
            days=args.days, chat_tg_id=args.chat, dry_run=args.dry_run, restart=args.restart
        )
    finally:
        await pool.close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nОстановлено. Повторный запуск продолжит с той же точки.")
