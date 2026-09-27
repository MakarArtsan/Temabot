"""Темы форума группы — чтобы владелец выбрал, куда публиковать дайджест.

Bot API списка тем не отдаёт, а без темы сообщение уходит в «Общую»; если она
закрыта, Telegram отвечает TOPIC_CLOSED и публикация не проходит. Коллектор
(user-сессия) видит темы и раз в несколько часов кладёт их в `state`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.db import repo
from src.db.models import Chat

log = logging.getLogger(__name__)

STATE_PREFIX = "topics:"
GENERAL_TOPIC_ID = 1   # «Общая» в MTProto; в Bot API у неё нет message_thread_id


def state_key(chat_id: int) -> str:
    return f"{STATE_PREFIX}{chat_id}"


def topic_rows(result: Any) -> list[dict[str, Any]]:
    """Ответ GetForumTopics -> [{id, title, closed}] без удалённых тем."""
    rows = []
    for topic in getattr(result, "topics", None) or []:
        title = getattr(topic, "title", None)
        if not title:
            continue   # ForumTopicDeleted
        rows.append({
            "id": int(topic.id),
            "title": str(title),
            "closed": bool(getattr(topic, "closed", False)),
        })
    return rows


async def sync_topics(client: Any, chat: Chat) -> list[dict[str, Any]] | None:
    """Записать темы группы. [] — в группе нет тем; None — узнать не удалось."""
    from telethon.tl.functions.messages import GetForumTopicsRequest

    entity = await client.get_entity(chat.tg_id)
    if not getattr(entity, "forum", False):
        rows: list[dict[str, Any]] = []
    else:
        result = await client(GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=100,
        ))
        rows = topic_rows(result)
    await repo.set_state(
        state_key(chat.id), {"at": datetime.now(UTC).isoformat(), "topics": rows}
    )
    return rows
