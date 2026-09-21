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


async def get_chat_by_id(chat_id: int) -> Chat | None:
    row = await pool.fetchrow("select * from chats where id = $1", chat_id)
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


async def find_chats(query: str) -> list[Chat]:
    """Поиск группы по куску названия — для `/ask #группа` (TZ §4.8)."""
    rows = await pool.fetch(
        """
        select * from chats
         where title ilike '%' || $1 || '%' or tg_id::text = $1
         order by title nulls last
        """,
        query,
    )
    return [Chat.from_row(r) for r in rows]


# ------------------------------------------------------------- блок-лист копировщика

async def block_user(tg_user_id: int, reason: str | None = None) -> None:
    await pool.execute(
        """
        insert into copier_blocklist (tg_user_id, reason) values ($1, $2)
        on conflict (tg_user_id) do update set reason = excluded.reason
        """,
        tg_user_id,
        reason,
    )


async def unblock_user(tg_user_id: int) -> bool:
    result = await pool.execute(
        "delete from copier_blocklist where tg_user_id = $1", tg_user_id
    )
    return result.endswith("1")


async def list_blocked() -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        select b.tg_user_id, b.reason, a.name
          from copier_blocklist b
          left join authors a on a.tg_user_id = b.tg_user_id
         order by b.created_at desc
        """
    )
    return [dict(r) for r in rows]


async def blocked_user_ids() -> set[int]:
    rows = await pool.fetch("select tg_user_id from copier_blocklist")
    return {int(r["tg_user_id"]) for r in rows}


async def find_author(query: str) -> Author | None:
    """Найти участника по id, @username или куску имени — для /ban (TZ §4.8)."""
    cleaned = query.strip().lstrip("@")
    row = await pool.fetchrow(
        """
        select * from authors
         where tg_user_id::text = $1
            or name ilike '@' || $1
            or name ilike '%' || $1 || '%'
         order by (tg_user_id::text = $1) desc, length(coalesce(name, ''))
         limit 1
        """,
        cleaned,
    )
    return Author.from_row(row) if row else None


# ------------------------------------------------------------------------ авторы

async def list_author_weights() -> dict[str, Any]:
    """Веса мнений и список заглушённых — для скоринга (TZ §4.7)."""
    rows = await pool.fetch("select tg_user_id, weight, muted from authors")
    return {
        "weights": {int(r["tg_user_id"]): float(r["weight"]) for r in rows},
        "muted": {int(r["tg_user_id"]) for r in rows if r["muted"]},
    }


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


async def mark_copied(
    *,
    chat_tg_id: int,
    tg_msg_id: int,
    text: str | None,
    tg_user_id: int | None,
    author_name: str | None,
    date: datetime,
) -> bool:
    """Пометить сообщение скопированным через бота (TZ §4.6).

    Если запись уже есть — её сохранил коллектор, значит трогаем только признаки
    копирования: `source` не меняем, текст не перезаписываем (у коллектора он
    полнее — с подписями к медиа и расшифровками).
    Если записи нет — вставляем с `source = 'copier'`: бот-копировщик может
    работать в группе, которую userbot не читает.

    Возвращает True, если строка уже существовала.
    """
    chat = await get_or_create_chat(chat_tg_id)
    if tg_user_id is not None:
        await upsert_author(tg_user_id, author_name)

    existed = await pool.fetchval(
        """
        insert into messages (chat_id, tg_msg_id, tg_user_id, author_name, text,
                              source, is_pinned_by_me, copy_count, date)
        values ($1, $2, $3, $4, $5, 'copier', true, 1, $6)
        on conflict (chat_id, tg_msg_id) do update set
            is_pinned_by_me = true,
            copy_count      = messages.copy_count + 1,
            author_name     = coalesce(messages.author_name, excluded.author_name),
            text            = coalesce(messages.text, excluded.text)
        returning (xmax <> 0) as existed
        """,
        chat.id,
        tg_msg_id,
        tg_user_id,
        author_name,
        text,
        date,
    )
    return bool(existed)


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


# ------------------------------------------------------- рейтинги участников (§4.10)

async def save_thread_contributions(
    chat_id: int, day: date_type, rows: list[dict[str, Any]]
) -> int:
    """Вклад участников в треды дня. Перезапись идемпотентна."""
    if not rows:
        return 0
    db = await pool.get_pool()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            "delete from thread_contrib where chat_id = $1 and day = $2", chat_id, day
        )
        for row in rows:
            await conn.execute(
                """
                insert into thread_contrib (chat_id, thread_id, tg_user_id, role, day)
                values ($1, $2, $3, $4, $5)
                on conflict do nothing
                """,
                chat_id,
                row["thread_id"],
                row["tg_user_id"],
                row["role"],
                day,
            )
    return len(rows)


async def get_thread_contributions(chat_id: int, day: date_type) -> list[dict[str, Any]]:
    """Вклад в треды дня вместе со скором темы — он нужен формуле полезности."""
    rows = await pool.fetch(
        """
        select c.thread_id, c.tg_user_id, c.role,
               coalesce(i.score, 0) as score, coalesce(i.shown, false) as shown
          from thread_contrib c
          left join digests d on d.chat_id = c.chat_id and d.day = c.day
          left join digest_items i on i.digest_id = d.id and i.thread_id = c.thread_id
         where c.chat_id = $1 and c.day = $2
        """,
        chat_id,
        day,
    )
    return [dict(r) for r in rows]


async def replace_author_stats(
    chat_id: int, day: date_type, rows: list[dict[str, Any]]
) -> int:
    """Записать дневную статистику. Пересчёт дня заменяет её целиком."""
    db = await pool.get_pool()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            "delete from author_stats_daily where chat_id = $1 and day = $2", chat_id, day
        )
        for row in rows:
            await conn.execute(
                """
                insert into author_stats_daily (
                    chat_id, tg_user_id, day, messages, short_msgs, words, longest_msg,
                    voice_sec, links, replies_got, reactions_got, questions_answered,
                    threads_started, night_msgs, usefulness
                ) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                chat_id,
                row["tg_user_id"],
                day,
                row["messages"],
                row["short_msgs"],
                row["words"],
                row["longest_msg"],
                row["voice_sec"],
                row["links"],
                row["replies_got"],
                row["reactions_got"],
                row["questions_answered"],
                row["threads_started"],
                row["night_msgs"],
                row["usefulness"],
            )
    return len(rows)


async def get_author_stats(
    chat_id: int | None,
    *,
    date_from: date_type,
    date_to: date_type,
    hide_optout: bool = True,
) -> list[dict[str, Any]]:
    """Суммарная статистика за период: неделя и месяц — это сумма по дням (§4.10)."""
    rows = await pool.fetch(
        """
        select s.tg_user_id,
               coalesce(a.name, s.tg_user_id::text) as name,
               sum(s.messages)   as messages,
               sum(s.short_msgs) as short_msgs,
               sum(s.words)      as words,
               max(s.longest_msg) as longest_msg,
               sum(s.voice_sec)  as voice_sec,
               sum(s.links)      as links,
               sum(s.replies_got) as replies_got,
               sum(s.reactions_got) as reactions_got,
               sum(s.questions_answered) as questions_answered,
               sum(s.threads_started) as threads_started,
               sum(s.night_msgs) as night_msgs,
               sum(s.usefulness) as usefulness
          from author_stats_daily s
          left join authors a on a.tg_user_id = s.tg_user_id
         where ($1::bigint is null or s.chat_id = $1)
           and s.day >= $2 and s.day <= $3
           and (not $4 or coalesce(a.hide_from_ratings, false) = false)
         group by s.tg_user_id, a.name
        """,
        chat_id,
        date_from,
        date_to,
        hide_optout,
    )
    return [dict(r) for r in rows]


async def set_hide_from_ratings(tg_user_id: int, hidden: bool = True) -> bool:
    """Команда /optout (TZ §4.10)."""
    result = await pool.execute(
        """
        insert into authors (tg_user_id, hide_from_ratings) values ($1, $2)
        on conflict (tg_user_id) do update set hide_from_ratings = excluded.hide_from_ratings
        """,
        tg_user_id,
        hidden,
    )
    return bool(result)


# ------------------------------------------------------------- данные для админки

async def messages_per_day(
    chat_id: int | None = None, *, days: int = 30, tz: str | None = None
) -> list[dict[str, Any]]:
    """Сообщений по дням — график на дашборде (TZ §4.9)."""
    rows = await pool.fetch(
        """
        select (date at time zone $3)::date as day, count(*) as count
          from messages
         where ($1::bigint is null or chat_id = $1)
           and deleted_at is null
           and date > now() - make_interval(days => $2)
         group by 1 order by 1
        """,
        chat_id,
        days,
        tz or cfg.TZ,
    )
    return [{"day": r["day"], "count": int(r["count"])} for r in rows]


async def tokens_per_day(days: int = 30, tz: str | None = None) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        select (created_at at time zone $2)::date as day,
               coalesce(sum(tokens_in), 0)  as tokens_in,
               coalesce(sum(tokens_out), 0) as tokens_out,
               count(*) as calls
          from llm_usage
         where created_at > now() - make_interval(days => $1)
         group by 1 order by 1
        """,
        days,
        tz or cfg.TZ,
    )
    return [dict(r) for r in rows]


async def pending_media_count(chat_id: int | None = None) -> int:
    """Очередь расшифровки — сколько голосовых ждут (TZ §4.9)."""
    value = await pool.fetchval(
        """
        select count(*) from messages
         where ($1::bigint is null or chat_id = $1)
           and media_type in ('voice', 'audio')
           and (transcript is null or transcript = '')
           and deleted_at is null
        """,
        chat_id,
    )
    return int(value or 0)


async def list_recent_digests(limit: int = 30) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        select d.id, d.chat_id, d.day, d.msg_count, d.tokens_used, d.created_at,
               c.title, c.tg_id,
               (select count(*) from digest_items i where i.digest_id = d.id and i.shown) as shown,
               (select count(*) from digest_items i where i.digest_id = d.id and not i.shown)
                   as missed
          from digests d left join chats c on c.id = d.chat_id
         order by d.day desc limit $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def list_authors(chat_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        select a.tg_user_id, a.name, a.weight, a.muted, a.hide_from_ratings,
               count(m.id) as messages,
               max(m.date) as last_at,
               (b.tg_user_id is not null) as blocked
          from authors a
          left join messages m on m.tg_user_id = a.tg_user_id
               and ($1::bigint is null or m.chat_id = $1)
          left join copier_blocklist b on b.tg_user_id = a.tg_user_id
         group by a.tg_user_id, a.name, a.weight, a.muted, a.hide_from_ratings, b.tg_user_id
         order by count(m.id) desc
         limit $2
        """,
        chat_id,
        limit,
    )
    return [dict(r) for r in rows]


async def set_author_flags(
    tg_user_id: int,
    *,
    weight: float | None = None,
    muted: bool | None = None,
    hide_from_ratings: bool | None = None,
) -> Author | None:
    row = await pool.fetchrow(
        """
        update authors
           set weight = coalesce($2, weight),
               muted  = coalesce($3, muted),
               hide_from_ratings = coalesce($4, hide_from_ratings),
               updated_at = now()
         where tg_user_id = $1
        returning *
        """,
        tg_user_id,
        weight,
        muted,
        hide_from_ratings,
    )
    return Author.from_row(row) if row else None


async def list_qa_log(limit: int = 50) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "select id, question, answer, sources, created_at from qa_log "
        "order by created_at desc limit $1",
        limit,
    )
    return [dict(r) for r in rows]


async def get_states(prefix: str = "") -> dict[str, Any]:
    """Heartbeat-и процессов для страницы «Система» (TZ §4.9)."""
    rows = await pool.fetch(
        "select key, value from state where key like $1 || '%'", prefix
    )
    return {r["key"]: r["value"] for r in rows}


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
    payload: dict[str, Any] | list[dict[str, Any]] | None = None,
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
        payload if payload is not None else {},
        msg_count,
        tokens_used,
    )
    return int(digest_id)


async def get_digest(chat_id: int, day: date_type) -> Digest | None:
    row = await pool.fetchrow(
        "select * from digests where chat_id = $1 and day = $2", chat_id, day
    )
    return Digest.from_row(row) if row else None


async def list_digests(chat_id: int, days: int = 7, until: date_type | None = None) -> list[Digest]:
    """Дайджесты за последние дни — основа для /week (TZ §4.5)."""
    last_day = until or date_type.today()
    rows = await pool.fetch(
        """
        select * from digests
         where chat_id = $1 and day > $2::date - $3::int and day <= $2::date
         order by day
        """,
        chat_id,
        last_day,
        days,
    )
    return [Digest.from_row(r) for r in rows]


# ---------------------------------------------------------------------- поиск

async def search_messages(
    query: str, *, chat_id: int | None = None, limit: int = 10
) -> list[Message]:
    """Полнотекстовый поиск по русской морфологии (TZ §4.5, /search).

    Ищем и по тексту, и по расшифровке голосовых — для читателя это одно и то же.
    """
    rows = await pool.fetch(
        """
        select *,
               ts_rank(
                   to_tsvector('russian', coalesce(text, '') || ' ' || coalesce(transcript, '')),
                   websearch_to_tsquery('russian', $1)
               ) as rank
          from messages
         where ($2::bigint is null or chat_id = $2)
           and deleted_at is null
           and to_tsvector('russian', coalesce(text, '') || ' ' || coalesce(transcript, ''))
               @@ websearch_to_tsquery('russian', $1)
         order by rank desc, date desc
         limit $3
        """,
        query,
        chat_id,
        limit,
    )
    return [Message.from_row(r) for r in rows]


# ------------------------------------------------------------------- чанки RAG

async def replace_chunks(chat_id: int, thread_id: int, rows: list[dict[str, Any]]) -> int:
    """Переиндексация треда: старые чанки заменяются новыми.

    Тред живёт и дополняется, поэтому индексация должна быть повторяемой —
    иначе в поиске копились бы дубли одного и того же обсуждения.
    """
    db = await pool.get_pool()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            "delete from chunks where chat_id = $1 and thread_id = $2", chat_id, thread_id
        )
        for row in rows:
            await conn.execute(
                """
                insert into chunks (chat_id, thread_id, msg_ids, text, embedding,
                                    date_from, date_to)
                values ($1, $2, $3, $4, $5::vector, $6, $7)
                """,
                chat_id,
                thread_id,
                row["msg_ids"],
                row["text"],
                row["embedding"],
                row.get("date_from"),
                row.get("date_to"),
            )
    return len(rows)


async def search_chunks_by_vector(
    embedding: str, *, chat_id: int | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    """Косинусная близость по pgvector (TZ §4.4)."""
    rows = await pool.fetch(
        """
        select id, chat_id, thread_id, msg_ids, text, date_from, date_to,
               1 - (embedding <=> $1::vector) as score
          from chunks
         where ($2::bigint is null or chat_id = $2) and embedding is not null
         order by embedding <=> $1::vector
         limit $3
        """,
        embedding,
        chat_id,
        limit,
    )
    return [dict(r) for r in rows]


async def search_chunks_by_text(
    query: str, *, chat_id: int | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    """Полнотекстовый поиск по чанкам — вторая половина гибрида.

    Слова вопроса соединяются через ИЛИ, а не через И. `websearch_to_tsquery`
    требует все слова сразу, и вопрос «сколько стоит генерация ролика» не находил
    обсуждение, где сказано «12 рублей за ролик»: слова «стоит» там нет.
    Ранжирование делает `ts_rank` — чем больше слов совпало, тем выше.
    """
    rows = await pool.fetch(
        """
        with q as (
            select to_tsquery(
                'russian',
                array_to_string(
                    tsvector_to_array(to_tsvector('russian', $1)), ' | '
                )
            ) as query
        )
        select c.id, c.chat_id, c.thread_id, c.msg_ids, c.text, c.date_from, c.date_to,
               ts_rank(to_tsvector('russian', c.text), q.query) as score
          from chunks c, q
         where ($2::bigint is null or c.chat_id = $2)
           and q.query is not null
           and to_tsvector('russian', c.text) @@ q.query
         order by score desc
         limit $3
        """,
        query,
        chat_id,
        limit,
    )
    return [dict(r) for r in rows]


async def get_thread_messages(
    chat_id: int, thread_id: int, *, limit: int = 40
) -> list[Message]:
    """Весь тред целиком — для расширения контекста (TZ §4.4)."""
    rows = await pool.fetch(
        """
        select * from messages
         where chat_id = $1 and thread_id = $2 and deleted_at is null
         order by date, tg_msg_id
         limit $3
        """,
        chat_id,
        thread_id,
        limit,
    )
    return [Message.from_row(r) for r in rows]


async def get_messages_around(
    chat_id: int, tg_msg_id: int, *, radius: int = 3
) -> list[Message]:
    """Соседние сообщения по времени — если тред ещё не размечен."""
    rows = await pool.fetch(
        """
        (select * from messages
          where chat_id = $1 and tg_msg_id <= $2 and deleted_at is null
          order by tg_msg_id desc limit $3)
        union
        (select * from messages
          where chat_id = $1 and tg_msg_id > $2 and deleted_at is null
          order by tg_msg_id limit $4)
        """,
        chat_id,
        tg_msg_id,
        radius + 1,   # само сообщение плюс radius до него
        radius,       # и radius после
    )
    return sorted(
        [Message.from_row(r) for r in rows], key=lambda m: (m.date, m.tg_msg_id)
    )


async def get_days_with_unassigned_messages(
    chat_id: int, *, tz: str | None = None, limit: int = 60
) -> list[date_type]:
    """Дни, где есть сообщения без thread_id.

    Скользящее окно «последние N дней» пропустило бы всю бэкфилленную историю,
    поэтому идём от данных: какие дни ещё не разложены по тредам.
    """
    rows = await pool.fetch(
        """
        select distinct (date at time zone $2)::date as day
          from messages
         where chat_id = $1 and thread_id is null and deleted_at is null
         order by day desc
         limit $3
        """,
        chat_id,
        tz or cfg.TZ,
        limit,
    )
    return [r["day"] for r in rows]


async def get_unindexed_threads(chat_id: int, limit: int = 200) -> list[int]:
    """Треды, которых ещё нет в индексе или которые успели дополниться."""
    rows = await pool.fetch(
        """
        select m.thread_id
          from messages m
          left join chunks c
                 on c.chat_id = m.chat_id and c.thread_id = m.thread_id
         where m.chat_id = $1 and m.thread_id is not null and m.deleted_at is null
         group by m.thread_id
        having count(c.id) = 0 or max(m.date) > max(coalesce(c.date_to, 'epoch'::timestamptz))
         limit $2
        """,
        chat_id,
        limit,
    )
    return [int(r["thread_id"]) for r in rows]


async def log_qa(question: str, answer: str, sources: list[int]) -> None:
    await pool.execute(
        "insert into qa_log (question, answer, sources) values ($1, $2, $3)",
        question,
        answer,
        sources,
    )


# ------------------------------------------------------------------ статистика

async def get_collection_stats(chat_id: int | None = None) -> dict[str, Any]:
    """Для команды /stats: сколько собрано, когда последнее, сколько потрачено."""
    row = await pool.fetchrow(
        """
        select count(*)                                as messages,
               count(*) filter (where transcript <> '') as transcribed,
               count(*) filter (where media_type = 'voice') as voices,
               max(date)                               as last_at,
               count(distinct tg_user_id)              as authors
          from messages
         where ($1::bigint is null or chat_id = $1) and deleted_at is null
        """,
        chat_id,
    )
    tokens = await pool.fetchrow(
        """
        select coalesce(sum(tokens_in), 0)  as tokens_in,
               coalesce(sum(tokens_out), 0) as tokens_out,
               count(*)                     as calls
          from llm_usage
         where ($1::bigint is null or chat_id = $1)
           and created_at > now() - interval '30 days'
        """,
        chat_id,
    )
    digests = await pool.fetchval(
        "select count(*) from digests where ($1::bigint is null or chat_id = $1)", chat_id
    )
    return {
        "messages": row["messages"] if row else 0,
        "transcribed": row["transcribed"] if row else 0,
        "voices": row["voices"] if row else 0,
        "authors": row["authors"] if row else 0,
        "last_at": row["last_at"] if row else None,
        "tokens_in": tokens["tokens_in"] if tokens else 0,
        "tokens_out": tokens["tokens_out"] if tokens else 0,
        "llm_calls": tokens["calls"] if tokens else 0,
        "digests": digests or 0,
    }


async def get_author_activity(
    name_or_id: str, *, chat_id: int | None = None, days: int = 30
) -> dict[str, Any] | None:
    """Для /who: кто это, сколько пишет, о чём (TZ §4.5)."""
    row = await pool.fetchrow(
        """
        select tg_user_id,
               max(author_name)  as name,
               count(*)          as messages,
               max(date)         as last_at,
               count(*) filter (where media_type = 'voice') as voices,
               count(*) filter (where reply_to is not null) as replies
          from messages
         where ($2::bigint is null or chat_id = $2)
           and deleted_at is null
           and date > now() - make_interval(days => $3)
           and (author_name ilike '%' || $1 || '%' or tg_user_id::text = $1)
         group by tg_user_id
         order by count(*) desc
         limit 1
        """,
        name_or_id,
        chat_id,
        days,
    )
    if row is None:
        return None
    return dict(row)


async def set_pinned(chat_id: int, tg_msg_id: int, pinned: bool = True) -> bool:
    """Команда /pin реплаем: пометить сообщение важным (TZ §4.5)."""
    result = await pool.execute(
        "update messages set is_pinned_by_me = $3 where chat_id = $1 and tg_msg_id = $2",
        chat_id,
        tg_msg_id,
        pinned,
    )
    return result.endswith("1")


# --------------------------------------------------- темы дайджеста и оценки (§4.7)

async def save_digest_items(
    digest_id: int, chat_id: int, items: list[dict[str, Any]]
) -> list[int]:
    """Записать темы дня вместе со всеми признаками.

    Повторный прогон за тот же день заменяет темы: иначе в админке и в `/missed`
    копились бы старые версии одного и того же дня.
    """
    db = await pool.get_pool()
    ids: list[int] = []
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("delete from digest_items where digest_id = $1", digest_id)
        for item in items:
            row_id = await conn.fetchval(
                """
                insert into digest_items (digest_id, chat_id, thread_id, title, kind,
                                          features, score, shown, embedding)
                values ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9::vector)
                returning id
                """,
                digest_id,
                chat_id,
                item.get("thread_id"),
                item.get("title"),
                item.get("kind"),
                item.get("features") or {},
                item.get("score"),
                item.get("shown", False),
                item.get("embedding"),
            )
            ids.append(int(row_id))
    return ids


async def get_digest_items(
    digest_id: int, *, shown: bool | None = None
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        select id, digest_id, chat_id, thread_id, title, kind, features, score, shown
          from digest_items
         where digest_id = $1 and ($2::bool is null or shown = $2)
         order by score desc nulls last
        """,
        digest_id,
        shown,
    )
    return [dict(r) for r in rows]


async def get_digest_item(item_id: int) -> dict[str, Any] | None:
    row = await pool.fetchrow(
        """
        select i.*, d.day, d.chat_id as digest_chat_id
          from digest_items i
          left join digests d on d.id = i.digest_id
         where i.id = $1
        """,
        item_id,
    )
    return dict(row) if row else None


async def recent_topic_embeddings(
    chat_id: int, *, days: int = 7, before: date_type | None = None
) -> list[list[float]]:
    """Эмбеддинги тем за последние дни — для проверки новизны (TZ §4.7)."""
    last_day = before or date_type.today()
    rows = await pool.fetch(
        """
        select i.embedding::text as embedding
          from digest_items i
          join digests d on d.id = i.digest_id
         where i.chat_id = $1
           and i.embedding is not null
           and d.day >= $2::date - $3::int and d.day < $2::date
         order by d.day desc
         limit 200
        """,
        chat_id,
        last_day,
        days,
    )
    result: list[list[float]] = []
    for row in rows:
        raw = row["embedding"]
        if not raw:
            continue
        result.append([float(x) for x in raw.strip("[]").split(",") if x])
    return result


async def signal_history(
    chat_id: int, *, days: int = 30, before: date_type | None = None
) -> dict[str, list[float]]:
    """Значения сигналов за последние дни — база для перцентилей (TZ §4.7)."""
    last_day = before or date_type.today()
    rows = await pool.fetch(
        """
        select i.features
          from digest_items i
          join digests d on d.id = i.digest_id
         where i.chat_id = $1
           and d.day >= $2::date - $3::int and d.day <= $2::date
         limit 2000
        """,
        chat_id,
        last_day,
        days,
    )
    history: dict[str, list[float]] = {}
    for row in rows:
        features = row["features"] or {}
        raw = features.get("raw_signals") or {}
        for name, value in raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                history.setdefault(name, []).append(float(value))
    return history


# ------------------------------------------------------------- обратная связь

async def add_feedback(item_id: int, value: int, note: str | None = None) -> int:
    """Оценка темы: +1 полезно, -1 мимо, -2 больше такое не показывать."""
    row_id = await pool.fetchval(
        """
        insert into feedback (item_id, value, note) values ($1, $2, $3)
        returning id
        """,
        item_id,
        value,
        note,
    )
    return int(row_id)


async def feedback_examples(
    chat_id: int, *, positive: int = 4, negative: int = 4
) -> list[dict[str, Any]]:
    """Последние оценки для few-shot в рубрике (TZ §4.7)."""
    rows = await pool.fetch(
        """
        (select f.value, i.title, i.features
           from feedback f join digest_items i on i.id = f.item_id
          where i.chat_id = $1 and f.value > 0
          order by f.created_at desc limit $2)
        union all
        (select f.value, i.title, i.features
           from feedback f join digest_items i on i.id = f.item_id
          where i.chat_id = $1 and f.value < 0
          order by f.created_at desc limit $3)
        """,
        chat_id,
        positive,
        negative,
    )
    examples = []
    for row in rows:
        features = row["features"] or {}
        examples.append(
            {
                "value": row["value"],
                "title": row["title"],
                "takeaway": features.get("takeaway", ""),
            }
        )
    return examples


async def feedback_dataset(chat_id: int | None = None) -> list[dict[str, Any]]:
    """Все оценки с признаками — обучающая выборка для пересчёта весов."""
    rows = await pool.fetch(
        """
        select f.value, i.features, i.score, i.chat_id
          from feedback f join digest_items i on i.id = f.item_id
         where ($1::bigint is null or i.chat_id = $1) and i.features is not null
         order by f.created_at
        """,
        chat_id,
    )
    return [dict(r) for r in rows]


async def count_feedback(chat_id: int | None = None) -> int:
    value = await pool.fetchval(
        """
        select count(*) from feedback f join digest_items i on i.id = f.item_id
         where ($1::bigint is null or i.chat_id = $1)
        """,
        chat_id,
    )
    return int(value or 0)


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
