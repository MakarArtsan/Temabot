"""Инкрементальная индексация тредов (TZ §4.4: раз в час, только новые).

Сегментация на треды живёт в памяти дайджеста, поэтому здесь она выполняется
заново по сообщениям из БД и её результат записывается в `messages.thread_id` —
так индекс, дайджест и рейтинги смотрят на одни и те же треды.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from src.db import repo
from src.db.models import Chat
from src.nlp.chunk import chunk_thread
from src.nlp.embed import embed_texts, to_pgvector
from src.nlp.threads import segment

log = logging.getLogger(__name__)

MAX_DAYS_PER_RUN = 60   # за один проход: чтобы бэкфилл на год не встал колом


@dataclass(slots=True)
class IndexResult:
    chat_tg_id: int
    threads: int = 0
    chunks: int = 0

    def describe(self) -> str:
        return f"чат {self.chat_tg_id}: тредов {self.threads}, чанков {self.chunks}"


async def assign_threads(chat: Chat, *, max_days: int = MAX_DAYS_PER_RUN) -> int:
    """Разметить сообщения по тредам и сохранить thread_id.

    Дни берутся из самих данных — те, где остались сообщения без разметки.
    Скользящее окно «последние N дней» пропустило бы всю историю, залитую
    бэкфиллом, и поиск знал бы только про последние сутки.

    День размечается целиком, а не только его новые сообщения: тред мог
    дополниться, и пересегментация должна видеть его полностью.
    """
    updated = 0
    days = await repo.get_days_with_unassigned_messages(chat.id, limit=max_days)
    for day in days:
        messages = await repo.get_messages_by_day(chat.id, day, include_deleted=False)
        if not messages:
            continue
        for thread in segment(messages):
            updated += await repo.set_thread_id(
                chat.id, thread.msg_ids, thread.root_msg_id
            )
    if updated:
        log.info("Размечено сообщений по тредам: %s (дней: %s)", updated, len(days))
    return updated


async def index_chat(chat: Chat, *, limit: int = 200) -> IndexResult:
    """Проиндексировать треды, которых нет в индексе или которые дополнились."""
    result = IndexResult(chat_tg_id=chat.tg_id)
    await assign_threads(chat)

    for thread_id in await repo.get_unindexed_threads(chat.id, limit=limit):
        messages = await repo.get_thread_messages(chat.id, thread_id, limit=200)
        if not messages:
            continue

        threads = segment(messages)
        if not threads:
            continue
        chunks = chunk_thread(threads[0])
        if not chunks:
            continue

        try:
            vectors = await embed_texts([c.text for c in chunks])
        except Exception:
            log.exception("Не удалось получить эмбеддинги для треда %s", thread_id)
            continue

        rows = [
            {
                "msg_ids": chunk.msg_ids,
                "text": chunk.text,
                "embedding": to_pgvector(vector),
                "date_from": chunk.date_from,
                "date_to": chunk.date_to,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        await repo.replace_chunks(chat.id, thread_id, rows)
        result.threads += 1
        result.chunks += len(rows)

    if result.threads:
        log.info("Проиндексировано: %s", result.describe())
    return result


async def index_all() -> list[IndexResult]:
    """Почасовая задача: пройти по всем группам со сбором."""
    results = []
    for chat in await repo.list_chats(collect=True):
        try:
            results.append(await index_chat(chat))
        except Exception:
            log.exception("Индексация группы %s не удалась", chat.tg_id)
    await repo.set_state(
        "rag:last_index",
        {"at": datetime.now(UTC).isoformat(), "chunks": sum(r.chunks for r in results)},
    )
    return results
