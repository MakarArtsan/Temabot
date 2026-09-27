"""Лор чата: местные мемы, легендарные персонажи, истории и роли участников.

Решение владельца (см. docs/PROGRESS.md): вести летопись того, что понимают
только свои. Раз в день, после сборки дайджеста, модель смотрит на все темы дня
(в том числе отсеянные — мемы часто рождаются во флуде) и нынешний лор и
решает, что добавить и что обновить. Её ответ проверяется: вид записи, длина,
номера сообщений и участники — только настоящие.

Лор видит только владелец (страница «Лор чата» в админке). Роли заводятся
лишь для тех, кто не скрылся из рейтингов (/optout). Мемы, персонажи и
истории подмешиваются в промпт разбора тредов — чтобы модель понимала отсылки.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from typing import Any

from src.db import repo
from src.db.models import Chat
from src.digest import prompts
from src.digest.render import Topic
from src.llm.client import chat_json

log = logging.getLogger(__name__)

KINDS = {"meme": "мем", "legend": "персонаж", "story": "история", "role": "роль"}
KIND_MARKS = {"meme": "😄", "legend": "🦸", "story": "📖", "role": "🎭"}
CONTEXT_LIMIT = 20       # сколько записей подмешивать в разбор каждого треда
PROMPT_LIMIT = 60        # сколько записей показывать модели при обновлении
MAX_ADDS_PER_DAY = 5
TITLE_MAX = 60
BODY_MAX = 400


def enabled(chat: Chat) -> bool:
    """Включено по умолчанию; выключается на странице «Лор чата»."""
    settings = (chat.settings or {}).get("lore") or {}
    return bool(settings.get("enabled", True))


async def context_lines(chat: Chat) -> str:
    """Мемы, персонажи и истории строками — для промпта разбора тредов.

    Роли людей сюда не идут: для пересказа они не нужны, а в текст дайджеста
    просочиться не должны.
    """
    if not enabled(chat):
        return ""
    rows = await repo.list_lore(chat.id)
    lines = [
        f"- {row['title']}: {row['body']}".rstrip(": ")
        for row in rows
        if row["kind"] != "role"
    ]
    return "\n".join(lines[:CONTEXT_LIMIT])


def _topic_line(topic: Topic) -> str:
    ids = sorted({topic.thread_id, *topic.key_msg_ids})
    people = ", ".join(topic.participants[:8])
    text = topic.summary or topic.gist or ""
    return (
        f"- [{topic.kind}] {topic.title}: {text}"
        + (f" (участники: {people})" if people else "")
        + f" [сообщения: {', '.join(map(str, ids))}]"
    )


def _existing_line(row: dict[str, Any]) -> str:
    body = str(row.get("body") or "")[:160]
    return f"{row['id']}. [{row['kind']}] {row['title']}" + (f" — {body}" if body else "")


async def update_lore(
    chat: Chat,
    day: date_type,
    topics: list[Topic],
    *,
    people: dict[int, str] | None = None,
    llm: Any = chat_json,
) -> list[str]:
    """Пополнить лор по темам дня. Возвращает строки о новых записях."""
    if not enabled(chat) or not topics:
        return []

    existing = await repo.list_lore(chat.id, include_hidden=True)
    opted_out = await repo.opted_out_ids()
    people = {uid: name for uid, name in (people or {}).items() if uid not in opted_out}

    user = prompts.LORE_USER.format(
        lore="\n".join(_existing_line(r) for r in existing[:PROMPT_LIMIT]) or "пока пусто",
        participants="\n".join(f"{uid} — {name}" for uid, name in people.items()) or "неизвестны",
        topics="\n".join(_topic_line(t) for t in topics),
    )
    data, _usage = await llm(
        [
            {"role": "system", "content": prompts.LORE_SYSTEM},
            {"role": "user", "content": user},
        ],
        purpose="lore",
        chat_id=chat.id,
    )
    if not isinstance(data, dict):
        return []

    known_ids = {int(r["id"]) for r in existing}
    msg_ids = {i for t in topics for i in (t.thread_id, *t.key_msg_ids)}
    added: list[str] = []

    for item in (data.get("add") or [])[:MAX_ADDS_PER_DAY]:
        entry = _clean_add(item, people=people, msg_ids=msg_ids)
        if entry is None:
            continue
        new_id = await repo.add_lore(
            chat.id,
            kind=entry["kind"],
            title=entry["title"],
            body=entry["body"],
            day=day,
            tg_user_id=entry["who"],
            sources=[{"day": day.isoformat(), "msg_id": i} for i in entry["msg_ids"]],
        )
        if new_id is not None:
            added.append(f"{KIND_MARKS[entry['kind']]} {KINDS[entry['kind']]} «{entry['title']}»")

    for item in data.get("update") or []:
        if not isinstance(item, dict):
            continue
        try:
            lore_id = int(str(item.get("id")))
        except (TypeError, ValueError):
            continue
        if lore_id not in known_ids:
            continue   # модель придумала номер — чужую запись не трогаем
        body = str(item.get("body") or "").strip()[:BODY_MAX]
        await repo.touch_lore(lore_id, chat.id, day=day, body=body)

    if added:
        log.info("Лор чата %s пополнился: %s", chat.tg_id, ", ".join(added))
    return added


def _clean_add(
    item: Any, *, people: dict[int, str], msg_ids: set[int]
) -> dict[str, Any] | None:
    """Проверить запись от модели: вид, длина, человек и сообщения — настоящие."""
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    title = str(item.get("title") or "").strip()[:TITLE_MAX]
    body = str(item.get("body") or "").strip()[:BODY_MAX]
    if kind not in KINDS or not title:
        return None

    who: int | None = None
    if kind == "role":
        try:
            who = int(str(item.get("who")))
        except (TypeError, ValueError):
            return None
        if who not in people:
            # не участник дня или скрылся по /optout — роль не заводим
            return None
        # одна роль — одна запись: «Вася — голос разума»
        title = f"{people[who]} — {title}"[:TITLE_MAX + 40]

    ids: list[int] = []
    for raw in item.get("msg_ids") or []:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number in msg_ids and number not in ids:
            ids.append(number)
    return {"kind": kind, "title": title, "body": body, "who": who, "msg_ids": ids[:3]}
