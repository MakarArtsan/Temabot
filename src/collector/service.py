"""Сервис коллектора: подписка на события и докачка пропущенного (TZ §4.1).

Один процесс — одна сессия. Вторая копия с той же сессией даёт
AUTH_KEY_DUPLICATED и Telegram может сбросить авторизацию (TZ шаг 13).
"""
from __future__ import annotations

import asyncio
import functools
import logging
from datetime import UTC, datetime
from typing import Any

from telethon import events

from src.collector.client import build_client, with_flood_retry
from src.collector.handlers import AuthorCache, normalize_message
from src.config import cfg
from src.db import repo
from src.db.models import Chat
from src.media.pipeline import MediaJob, MediaQueue, NullMediaQueue

log = logging.getLogger(__name__)

CATCHUP_BATCH = 200          # порция докачки, как в бэкфилле (TZ шаг 4)
CATCHUP_PAUSE_SEC = 1.5      # пауза между порциями, чтобы не ловить FloodWait
HEARTBEAT_SEC = 60           # для страницы «Система» в админке (TZ §4.9)
HEARTBEAT_KEY = "collector:heartbeat"


class Collector:
    """Сбор сообщений в БД. В dry-run печатает в консоль и не пишет ничего."""

    def __init__(
        self,
        client: Any,
        *,
        dry_run: bool = False,
        media: MediaQueue | NullMediaQueue | None = None,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.media = media or NullMediaQueue()
        self.authors = AuthorCache()
        self._chats_by_tg_id: dict[int, Chat] = {}
        self.saved = 0

    # ------------------------------------------------------------- целевые чаты

    async def load_target_chats(self) -> list[Chat]:
        """Только чаты с collect = true (TZ §4.8).

        Группа из TG_GROUP_ID при первом запуске заводится сразу рабочей
        (сбор, дайджест, копировщик) — иначе collector стартовал бы вхолостую.
        Если строка уже есть, её флаги не трогаем: их меняет владелец в админке.
        """
        if cfg.TG_GROUP_ID:
            await repo.bootstrap_primary_chat(cfg.TG_GROUP_ID)
        chats = await repo.list_chats(collect=True)
        self._chats_by_tg_id = {c.tg_id: c for c in chats}
        return chats

    def chat_for(self, chat_tg_id: int) -> Chat | None:
        return self._chats_by_tg_id.get(chat_tg_id)

    # ------------------------------------------------------------- запись в БД

    async def save(self, message: Any, chat: Chat) -> None:
        author_name = await self.authors.resolve(message, persist=not self.dry_run)
        row = normalize_message(message, chat.id, author_name=author_name)

        if self.dry_run:
            preview = (row.text or "")[:80].replace("\n", " ")
            print(
                f"[dry-run] {row.date:%Y-%m-%d %H:%M} #{row.tg_msg_id} "
                f"{author_name or row.tg_user_id}: {preview}"
                + (f" [{row.media_type}]" if row.media_type else "")
            )
            self.saved += 1
            return

        await repo.upsert_message(row)
        self.saved += 1

        if row.media_type:
            # расшифровка идёт в фоне: приём сообщений не ждёт трёхминутное голосовое
            self.media.submit(
                MediaJob(
                    chat_id=chat.id,
                    chat_tg_id=chat.tg_id,
                    tg_msg_id=row.tg_msg_id,
                    media_type=row.media_type,
                    message=message,
                )
            )

    # ---------------------------------------------------------------- события

    async def on_new_message(self, event: Any) -> None:
        chat = self.chat_for(event.chat_id)
        if chat is None:
            return
        try:
            await self.save(event.message, chat)
        except Exception:
            log.exception("Не удалось сохранить сообщение %s", getattr(event, "id", "?"))

    async def on_message_edited(self, event: Any) -> None:
        chat = self.chat_for(event.chat_id)
        if chat is None or self.dry_run:
            return
        message = event.message
        await repo.update_message_text(
            chat.id,
            int(message.id),
            getattr(message, "message", None) or None,
            getattr(message, "edit_date", None),
        )

    async def on_message_deleted(self, event: Any) -> None:
        """Мягкое удаление: дайджест за день должен остаться честным (TZ §4.1)."""
        chat = self.chat_for(event.chat_id) if event.chat_id else None
        ids = [int(i) for i in (event.deleted_ids or [])]
        if not ids or self.dry_run:
            return
        if chat is not None:
            await repo.soft_delete_messages(chat.id, ids)
            return
        # DeletedMessage в личных чатах приходит без chat_id — чистим по всем целевым
        for target in self._chats_by_tg_id.values():
            await repo.soft_delete_messages(target.id, ids)

    # ------------------------------------------------------------- докачка

    async def catch_up(self, chat: Chat) -> int:
        """Дочитать пропущенное за время простоя (TZ §4.1).

        Опорная точка — максимальный tg_msg_id в БД: он не расходится с
        реальностью, даже если процесс упал, не успев обновить state.
        """
        last_id = await repo.get_last_tg_msg_id(chat.id)
        if not last_id:
            log.info("Чат %s пуст — докачка пропущена (история: BACKFILL_DAYS)", chat.tg_id)
            return 0

        fetched = 0
        batch: list[Any] = []
        iterator = self.client.iter_messages(chat.tg_id, min_id=last_id, reverse=True)
        async for message in iterator:
            batch.append(message)
            if len(batch) >= CATCHUP_BATCH:
                fetched += await self._save_batch(batch, chat)
                batch = []
                await asyncio.sleep(CATCHUP_PAUSE_SEC)
        if batch:
            fetched += await self._save_batch(batch, chat)

        if fetched:
            log.info("Докачано %s сообщений в чате %s (после #%s)", fetched, chat.tg_id, last_id)
        return fetched

    async def _save_batch(self, batch: list[Any], chat: Chat) -> int:
        saved = 0
        for message in batch:
            try:
                await self.save(message, chat)
                saved += 1
            except Exception:
                log.exception("Пропускаю сообщение %s", getattr(message, "id", "?"))
        return saved

    # ------------------------------------------------------------- heartbeat

    async def heartbeat_loop(self, interval: int = HEARTBEAT_SEC) -> None:
        while True:
            if not self.dry_run:
                await repo.set_state(
                    HEARTBEAT_KEY,
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "saved": self.saved,
                        "media": self.media.stats.as_dict(),
                    },
                )
            await asyncio.sleep(interval)


def register_handlers(collector: Collector, chat_ids: list[int]) -> None:
    client = collector.client
    client.add_event_handler(collector.on_new_message, events.NewMessage(chats=chat_ids))
    client.add_event_handler(collector.on_message_edited, events.MessageEdited(chats=chat_ids))
    client.add_event_handler(collector.on_message_deleted, events.MessageDeleted())


def build_media_queue(*, dry_run: bool) -> MediaQueue | NullMediaQueue:
    """Очередь расшифровки или заглушка (ASR_ENABLED=false, TZ шаг 13)."""
    if dry_run or not (cfg.ASR_ENABLED or cfg.VISION_ENABLED):
        log.info("Обработка медиа выключена")
        return NullMediaQueue()

    from src.media.image import describe_image
    from src.media.telegram_asr import transcribe_message
    from src.media.voice import transcribe

    # При ASR_PROVIDER=local к Telegram не обращаемся вовсе
    telegram_asr = None if cfg.ASR_PROVIDER == "local" else transcribe_message
    if telegram_asr is not None:
        log.info("Расшифровка: сначала силами Telegram (%s)", cfg.ASR_PROVIDER)

    return MediaQueue(
        transcribe, telegram_transcriber=telegram_asr, describer=describe_image
    )


async def requeue_pending_media(collector: Collector, chat: Chat, limit: int = 200) -> int:
    """Догнать голосовые, оставшиеся без расшифровки.

    Очередь живёт в памяти, поэтому рестарт посреди обработки теряет задания.
    В БД такие сообщения видно: media_type = voice, а transcript пуст.
    """
    pending = await repo.get_pending_transcriptions(chat.id, limit)
    if not pending:
        return 0

    queued = 0
    for row in pending:
        message = await collector.client.get_messages(chat.tg_id, ids=row.tg_msg_id)
        if message is None:
            continue
        job = MediaJob(
            chat_id=chat.id,
            chat_tg_id=chat.tg_id,
            tg_msg_id=row.tg_msg_id,
            media_type=row.media_type or "voice",
            message=message,
        )
        if collector.media.submit(job):
            queued += 1
    if queued:
        log.info("В очередь на расшифровку возвращено %s сообщений", queued)
    return queued


async def connect(client: Any) -> None:
    """Подключиться и убедиться, что сессия живая.

    `client.start()` при недействительной сессии спрашивает телефон через
    input(): на сервере это падение с EOFError и непонятной ошибкой в логе.
    """
    await with_flood_retry(client.connect, description="подключение к Telegram")
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(
            "Сессия Telegram недействительна или не задана. Сгенерируй новую "
            "строку (make login / docs/SETUP.md) и положи её в TG_SESSION_STRING."
        )


async def auto_backfill(collector: Collector, chat: Chat, days: int) -> None:
    """Разовая заливка истории при первом старте (TZ §4.1, шаг 4).

    Прогресс хранится в state, поэтому после рестарта заливка продолжается,
    а пройденный чат повторно не читается. Ошибка не валит collector:
    сбор новых сообщений важнее старой истории.
    """
    from src.collector.backfill import backfill_chat

    if days <= 0 or collector.dry_run:
        return
    try:
        result = await backfill_chat(collector, chat, days=days)
    except Exception:
        log.exception("Заливка истории чата %s прервалась, продолжу при рестарте", chat.tg_id)
        return
    if result.batches:
        log.info("Заливка истории за %s дн.: %s", days, result.describe())


async def run(*, dry_run: bool = False) -> None:
    client = build_client()
    media = build_media_queue(dry_run=dry_run)
    collector = Collector(client, dry_run=dry_run, media=media)

    await connect(client)
    me = await client.get_me()
    log.info("Вошли как %s (id=%s)", getattr(me, "username", None) or me.id, me.id)

    chats = await collector.load_target_chats()
    if not chats:
        raise SystemExit(
            "Нет ни одной группы с collect=true. Укажи TG_GROUP_ID "
            "или включи группу в админке."
        )
    log.info("Слушаю чаты: %s", [c.tg_id for c in chats])

    # очередь медиа нужна уже во время заливки: голосовые из истории тоже расшифруем
    await media.start()
    for chat in chats:
        await auto_backfill(collector, chat, cfg.BACKFILL_DAYS)
        await with_flood_retry(
            functools.partial(collector.catch_up, chat),
            description=f"докачка чата {chat.tg_id}",
        )

    for chat in chats:
        await requeue_pending_media(collector, chat)

    register_handlers(collector, [c.tg_id for c in chats])
    heartbeat = asyncio.create_task(collector.heartbeat_loop())
    try:
        await client.run_until_disconnected()
    finally:
        heartbeat.cancel()
        await media.stop()
