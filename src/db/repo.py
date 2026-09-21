"""Доступ к БД: единственное место в проекте, где есть SQL (CLAUDE.md).

Все функции принимают внутренний `chat_id` (или `chat_tg_id` там, где вызывающий
знает только телеграмный id) и ничего не знают о бизнес-логике.
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.config import cfg
from src.db import pool
from src.db.models import Author, Chat, Digest, Message

# ------------------------------------------------------------------ вспомогательное


def day_bounds(day: date_type, tz: str | None = None) -> tuple[datetime, datetime]:
    """Границы локальных суток [00:00, 24:00) в виде aware-datetime (TZ §4.3)."""
    zone = ZoneInfo(tz or cfg.TZ)
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


# ------------------------------------------------------------------------- чаты

async def get_chat_by_tg_id(chat_tg_id: int) -> Chat | None:
    row = await pool.fetchrow("select * from chats where tg_id = $1", chat_tg_id)
    return Chat.from_row(row) if row else None


async def get_or_create_chat(chat_tg_id: int, title: str | None = None) -> Chat:
    """Новые чаты появляются выключенными — включает владелец в админке (TZ §4.8)."""
    row = await pool.fetchrow(
        """
        insert into chats (tg_id, title)
        values ($1, $2)
        on conflict (tg_id) do update
            set title = coalesce(excluded.title, chats.title)
        returning *
        """,
        chat_tg_id,
        title,
    )
    assert row is not None
    return Chat.from_row(row)


async def list_chats(*, collect: bool | None = None, digest: bool | None = None) -> list[Chat]:
    rows = await pool.fetch(
        """
        select * from chats
        where ($1::bool is null or collect = $1)
          and ($2::bool is null or digest = $2)
        order by title nulls last, tg_id
        """,
        collect,
        digest,
    )
    return [Chat.from_row(r) for r in rows]


async def set_chat_flags(
    chat_tg_id: int,
    *,
    collect: bool | None = None,
    digest: bool | None = None,
    copier: str | None = None,
) -> Chat | None:
    row = await pool.fetchrow(
        """
        update chats
           set collect = coalesce($2, collect),
               digest  = coalesce($3, digest),
               copier  = coalesce($4, copier)
         where tg_id = $1
        returning *
        """,
        chat_tg_id,
        collect,
        digest,
        copier,
    )
    return Chat.from_row(row) if row else None


async def update_chat_settings(chat_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    """Мержит patch в chats.settings (веса, пороги, профиль интересов — TZ §4.7)."""
    value = await pool.fetchval(
        """
        update chats
           set settings = coalesce(settings, '{}'::jsonb) || $2::jsonb
         where id = $1
        returning settings
        """,
        chat_id,
        patch,
    )
    return dict(value or {})


# ------------------------------------------------------------------------ авторы

async def upsert_author(tg_user_id: int, name: str | None) -> Author:
    row = await pool.fetchrow(
        """
        insert into authors (tg_user_id, name)
        values ($1, $2)
        on conflict (tg_user_id) do update
            set name = coalesce(excluded.name, authors.name),
                updated_at = now()
        returning *
        """,
        tg_user_id,
        name,
    )
    assert row is not None
    return Author.from_row(row)


# --------------------------------------------------------------------- сообщения

async def upsert_message(msg: Message) -> int:
    """Вставка/обновление сообщения по (chat_id, tg_msg_id).

    Поля, которые проставляют другие процессы, при повторной вставке не затираются:
    копировщик пишет is_pinned_by_me/copy_count, расшифровка — transcript,
    сегментация — thread_id. Бэкфилл поверх собранного ничего не ломает.
    """
    row_id = await pool.fetchval(
        """
        insert into messages (
            chat_id, tg_msg_id, tg_user_id, author_name, text, transcript,
            media_type, media_path, reply_to, topic_id, thread_id, source,
            is_pinned_by_me, copy_count, reactions, reply_count,
            date, edited_at, raw
        )
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16, $17, $18, $19)
        on conflict (chat_id, tg_msg_id) do update set
            tg_user_id    = coalesce(excluded.tg_user_id, messages.tg_user_id),
            author_name   = coalesce(excluded.author_name, messages.author_name),
            text          = coalesce(excluded.text, messages.text),
            transcript    = coalesce(excluded.transcript, messages.transcript),
            media_type    = coalesce(excluded.media_type, messages.media_type),
            media_path    = coalesce(excluded.media_path, messages.media_path),
            reply_to      = coalesce(excluded.reply_to, messages.reply_to),
            topic_id      = coalesce(excluded.topic_id, messages.topic_id),
            thread_id     = coalesce(excluded.thread_id, messages.thread_id),
            is_pinned_by_me = messages.is_pinned_by_me or excluded.is_pinned_by_me,
            copy_count    = greatest(messages.copy_count, excluded.copy_count),
            reactions     = greatest(messages.reactions, excluded.reactions),
            reply_count   = greatest(messages.reply_count, excluded.reply_count),
            edited_at     = coalesce(excluded.edited_at, messages.edited_at),
            raw           = coalesce(excluded.raw, messages.raw)
        returning id
        """,
        msg.chat_id,
        msg.tg_msg_id,
        msg.tg_user_id,
        msg.author_name,
        msg.text,
        msg.transcript,
        msg.media_type,
        msg.media_path,
        msg.reply_to,
        msg.topic_id,
        msg.thread_id,
        msg.source,
        msg.is_pinned_by_me,
        msg.copy_count,
        msg.reactions,
        msg.reply_count,
        msg.date,
        msg.edited_at,
        msg.raw or None,
    )
    return int(row_id)


async def update_message_text(
    chat_id: int, tg_msg_id: int, text: str | None, edited_at: datetime | None = None
) -> bool:
    """MessageEdited (TZ §4.1)."""
    result = await pool.execute(
        """
        update messages
           set text = $3, edited_at = coalesce($4, now())
         where chat_id = $1 and tg_msg_id = $2
        """,
        chat_id,
        tg_msg_id,
        text,
        edited_at,
    )
    return result.endswith("1")


async def soft_delete_messages(chat_id: int, tg_msg_ids: list[int]) -> int:
    """MessageDeleted: флаг, а не DELETE — дайджест за день должен остаться честным."""
    value = await pool.fetchval(
        """
        with upd as (
            update messages set deleted_at = now()
             where chat_id = $1 and tg_msg_id = any($2::bigint[]) and deleted_at is null
            returning 1
        )
        select count(*) from upd
        """,
        chat_id,
        tg_msg_ids,
    )
    return int(value or 0)


async def set_transcript(chat_id: int, tg_msg_id: int, transcript: str) -> bool:
    result = await pool.execute(
        "update messages set transcript = $3 where chat_id = $1 and tg_msg_id = $2",
        chat_id,
        tg_msg_id,
        transcript,
    )
    return result.endswith("1")


async def set_media_path(chat_id: int, tg_msg_id: int, media_path: str) -> bool:
    result = await pool.execute(
        "update messages set media_path = $3 where chat_id = $1 and tg_msg_id = $2",
        chat_id,
        tg_msg_id,
        media_path,
    )
    return result.endswith("1")


async def get_pending_transcriptions(chat_id: int, limit: int = 200) -> list[Message]:
    """Голосовые без расшифровки: очередь живёт в памяти и теряется при рестарте."""
    rows = await pool.fetch(
        """
        select * from messages
         where chat_id = $1
           and media_type in ('voice', 'audio')
           and (transcript is null or transcript = '')
           and deleted_at is null
         order by date desc
         limit $2
        """,
        chat_id,
        limit,
    )
    return [Message.from_row(r) for r in rows]


async def set_thread_id(chat_id: int, tg_msg_ids: list[int], thread_id: int) -> int:
    value = await pool.fetchval(
        """
        with upd as (
            update messages set thread_id = $3
             where chat_id = $1 and tg_msg_id = any($2::bigint[])
            returning 1
        )
        select count(*) from upd
        """,
        chat_id,
        tg_msg_ids,
        thread_id,
    )
    return int(value or 0)


async def get_messages_by_day(
    chat_id: int, day: date_type, *, tz: str | None = None, include_deleted: bool = True
) -> list[Message]:
    """Сообщения за локальные сутки. Удалённые по умолчанию включены (TZ §4.1)."""
    start, end = day_bounds(day, tz)
    rows = await pool.fetch(
        """
        select * from messages
         where chat_id = $1 and date >= $2 and date < $3
           and ($4 or deleted_at is null)
         order by date, tg_msg_id
        """,
        chat_id,
        start,
        end,
        include_deleted,
    )
    return [Message.from_row(r) for r in rows]


async def get_message(chat_id: int, tg_msg_id: int) -> Message | None:
    row = await pool.fetchrow(
        "select * from messages where chat_id = $1 and tg_msg_id = $2", chat_id, tg_msg_id
    )
    return Message.from_row(row) if row else None


async def get_last_tg_msg_id(chat_id: int) -> int | None:
    """Для докачки пропущенного при рестарте (TZ §4.1)."""
    return await pool.fetchval(
        "select max(tg_msg_id) from messages where chat_id = $1", chat_id
    )


async def count_messages(chat_id: int | None = None) -> int:
    value = await pool.fetchval(
        "select count(*) from messages where ($1::bigint is null or chat_id = $1)", chat_id
    )
    return int(value or 0)


# ------------------------------------------------------------------------ state

async def get_state(key: str, default: Any = None) -> Any:
    value = await pool.fetchval("select value from state where key = $1", key)
    return default if value is None else value


async def set_state(key: str, value: Any) -> None:
    await pool.execute(
        """
        insert into state (key, value) values ($1, $2::jsonb)
        on conflict (key) do update set value = excluded.value
        """,
        key,
        value,
    )


# --------------------------------------------------------------------- дайджесты

async def save_digest(
    chat_id: int,
    day: date_type,
    summary_md: str,
    topics: list[dict[str, Any]] | None = None,
    msg_count: int = 0,
    tokens_used: int = 0,
) -> int:
    """Идемпотентно: повторный прогон за тот же день перезаписывает запись (TZ §4.3)."""
    digest_id = await pool.fetchval(
        """
        insert into digests (chat_id, day, summary_md, topics, msg_count, tokens_used)
        values ($1, $2, $3, $4::jsonb, $5, $6)
        on conflict (chat_id, day) do update set
            summary_md  = excluded.summary_md,
            topics      = excluded.topics,
            msg_count   = excluded.msg_count,
            tokens_used = excluded.tokens_used,
            created_at  = now()
        returning id
        """,
        chat_id,
        day,
        summary_md,
        topics or [],
        msg_count,
        tokens_used,
    )
    return int(digest_id)


async def get_digest(chat_id: int, day: date_type) -> Digest | None:
    row = await pool.fetchrow(
        "select * from digests where chat_id = $1 and day = $2", chat_id, day
    )
    return Digest.from_row(row) if row else None


async def log_llm_usage(
    *,
    chat_id: int | None,
    purpose: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float | None = None,
) -> None:
    await pool.execute(
        """
        insert into llm_usage (chat_id, purpose, model, tokens_in, tokens_out, cost_usd)
        values ($1, $2, $3, $4, $5, $6)
        """,
        chat_id,
        purpose,
        model,
        tokens_in,
        tokens_out,
        cost_usd,
    )
