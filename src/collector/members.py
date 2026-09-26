"""Кто состоит в группе — для страницы участников (TZ §9, решение владельца).

Веб спрашивает членство у Bot API (getChatMember), но Bot API гарантирует
ответ, только если бот — администратор группы, а при скрытом списке участников
обычный бот видит не всех. Коллектор читает группу от имени аккаунта-участника,
поэтому держит собственный список: это запасной источник для проверки доступа.

Храним только id участников — ни имён, ни чего-то ещё.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from src.collector.client import with_flood_retry
from src.db import repo
from src.db.models import Chat

log = logging.getLogger(__name__)

SYNC_EVERY_SEC = 6 * 3600     # полная сверка; вход и выход между сверками ловят события


def _is_admin(entity: Any) -> bool:
    return bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))


async def _participants_hidden(client: Any, entity: Any) -> bool:
    """Скрыт ли список участников от обычных членов группы."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    try:
        full = await client(GetFullChannelRequest(entity))
    except Exception:
        # обычная (не супер-) группа или нет прав: список там не скрывается
        return False
    return bool(getattr(full.full_chat, "participants_hidden", False))


async def sync_members(client: Any, chat: Chat) -> int | None:
    """Сверить список участников. None — сверить не удалось, старый список не тронут."""
    try:
        entity = await client.get_entity(chat.tg_id)
        if await _participants_hidden(client, entity) and not _is_admin(entity):
            # Telegram отдал бы только админов — такой список выкинул бы всех остальных
            log.info("Список участников %s скрыт, сверка пропущена", chat.tg_id)
            return None
        users = await with_flood_retry(
            functools.partial(client.get_participants, entity),
            description=f"участники чата {chat.tg_id}",
        )
    except Exception:
        log.warning("Не удалось получить участников %s", chat.tg_id, exc_info=True)
        return None

    ids = [
        int(u.id)
        for u in users
        if not getattr(u, "bot", False) and not getattr(u, "deleted", False)
    ]
    if not ids:
        # в группе как минимум сам аккаунт коллектора — пустой ответ подозрителен
        log.info("Telegram не отдал участников %s, список не трогаю", chat.tg_id)
        return None
    total = int(getattr(users, "total", 0) or 0)
    complete = len(users) >= total
    await repo.save_chat_members(chat.id, ids, complete=complete)
    log.info(
        "Участники %s: %s%s", chat.tg_id, len(ids), "" if complete else f" из {total}"
    )
    return len(ids)


async def sync_loop(client: Any, chats: list[Chat], every_sec: float = SYNC_EVERY_SEC) -> None:
    """Сверять участников всех групп раз в несколько часов. Ошибки не роняют коллектор."""
    while True:
        for chat in chats:
            try:
                await sync_members(client, chat)
            except Exception:
                log.exception("Сверка участников %s упала", chat.tg_id)
        await asyncio.sleep(every_sec)


async def on_membership_change(chat: Chat, event: Any) -> None:
    """Вход и выход между сверками: доступ появляется и пропадает сразу."""
    user_ids = [int(uid) for uid in (getattr(event, "user_ids", None) or [])]
    if not user_ids:
        return
    if getattr(event, "user_joined", False) or getattr(event, "user_added", False):
        for uid in user_ids:
            await repo.add_chat_member(chat.id, uid)
    elif getattr(event, "user_left", False) or getattr(event, "user_kicked", False):
        for uid in user_ids:
            await repo.remove_chat_member(chat.id, uid)
