"""Досье участников: важные вехи и характеристика (решение владельца, 30.10).

Раз в день, после сборки дайджеста, модель смотрит темы дня (и отсеянные тоже —
победы часто тонут в поздравлениях) и то, что уже известно о людях этого дня, и
решает, какие вехи записать и чью характеристику обновить. Характеристика всегда
доброжелательная, можно с лёгким юмором; здоровье, деньги, ссоры и прочее, что
человеку было бы неприятно увидеть, не записываются (`prompts.DOSSIER_SYSTEM`).

По умолчанию досье видит только владелец (страница «Досье» в админке). Участникам
оно открывается галочкой (`settings.dossier.public`) на их странице, без тех,
кто скрылся по /optout. Скрывшимся по /optout досье не заводим вовсе.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date as date_type
from datetime import time, timedelta
from typing import Any

from src.db import repo
from src.db.models import Chat
from src.digest import prompts
from src.digest.render import Topic
from src.llm.client import chat_json

log = logging.getLogger(__name__)

BACKFILL_DAYS = 60          # «Собрать по архиву»: весь архив выпусков за два месяца
MAX_PEOPLE_PROMPT = 40      # людей дня в промпте — самые заметные
MAX_PEOPLE_PER_DAY = 15     # чьи досье модель может тронуть за день
MAX_MILESTONES_PER_DAY = 3  # вех у одного человека за день
KNOWN_MILESTONES = 5        # сколько последних вех показывать модели
MILESTONE_MAX = 200
SUMMARY_MAX = 700
TOPIC_TEXT_MAX = 400


def settings_of(chat: Chat) -> dict[str, Any]:
    return dict((chat.settings or {}).get("dossier") or {})


def enabled(chat: Chat) -> bool:
    """Вести досье — по умолчанию да; выключается на странице «Досье»."""
    return bool(settings_of(chat).get("enabled", True))


def public(chat: Chat) -> bool:
    """Видно ли досье участникам — по умолчанию нет, открывает владелец галочкой."""
    return bool(settings_of(chat).get("public", False))


def _name_key(name: str) -> str:
    """Имя для поиска в тексте: первое слово от трёх букв («Дария byEDINOROG» → «дария»)."""
    for word in re.findall(r"[\w-]+", name.lower()):
        if len(word) >= 3:
            return word
    return ""


def day_cast(topics: list[Topic], people: dict[int, str]) -> dict[int, str]:
    """Люди дня, о которых что-то есть в обсуждениях: по участникам и упоминаниям."""
    mentioned: dict[int, int] = {}
    texts = [
        " ".join([t.title, t.summary or t.gist or "", " ".join(t.participants)]).lower()
        for t in topics
    ]
    by_id = {int(c["tg_user_id"]) for t in topics for c in t.contributors
             if isinstance(c, dict) and c.get("tg_user_id")}
    for uid, name in people.items():
        key = _name_key(name)
        hits = sum(1 for text in texts if key and key in text)
        if uid in by_id:
            hits += 1
        if hits:
            mentioned[uid] = hits
    top = sorted(mentioned, key=lambda uid: -mentioned[uid])[:MAX_PEOPLE_PROMPT]
    return {uid: people[uid] for uid in top}


def _topic_line(topic: Topic) -> str:
    ids = sorted({topic.thread_id, *topic.key_msg_ids})
    text = (topic.summary or topic.gist or "").replace("\n", " ")[:TOPIC_TEXT_MAX]
    people = ", ".join(topic.participants[:8])
    return (f"- {topic.title}: {text}"
            + (f" (участники: {people})" if people else "")
            + f" [сообщения: {', '.join(map(str, ids))}]")


def _person_line(uid: int, name: str, known: dict[str, Any] | None) -> str:
    line = f"{uid} — {name}"
    if known:
        if known.get("summary"):
            line += f"\n    характеристика: {known['summary']}"
        recent = [str(m.get("text", "")) for m in (known.get("milestones") or [])]
        if recent:
            line += "\n    вехи: " + "; ".join(recent[-KNOWN_MILESTONES:])
    return line


async def update_dossiers(
    chat: Chat,
    day: date_type,
    topics: list[Topic],
    people: dict[int, str],
    *,
    llm: Any = chat_json,
) -> list[str]:
    """Пополнить досье по темам дня. Возвращает имена тех, чьи досье обновились."""
    material = [t for t in topics if t.summary or t.gist]
    if not enabled(chat) or not material or not people:
        return []
    opted_out = await repo.opted_out_ids()
    cast = day_cast(material, {u: n for u, n in people.items() if u not in opted_out})
    if not cast:
        return []
    known = {int(r["tg_user_id"]): r
             for r in await repo.list_dossiers(chat.id, include_hidden=True)}
    hidden = {uid for uid, row in known.items() if row.get("hidden")}
    cast = {uid: name for uid, name in cast.items() if uid not in hidden}
    if not cast:
        return []

    system = prompts.DOSSIER_SYSTEM.replace("{day}", f"{day:%d.%m.%Y}")
    data, _usage = await llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompts.DOSSIER_USER.format(
                people="\n".join(_person_line(uid, name, known.get(uid))
                                 for uid, name in cast.items()),
                topics="\n".join(_topic_line(t) for t in material),
            )},
        ],
        purpose="dossier",
        chat_id=chat.id,
    )
    if not isinstance(data, dict):
        return []

    msg_ids = {i for t in material for i in (t.thread_id, *t.key_msg_ids)}
    touched: list[str] = []
    for item in (data.get("people") or [])[:MAX_PEOPLE_PER_DAY]:
        if not isinstance(item, dict):
            continue
        try:
            uid = int(str(item.get("who")))
        except (TypeError, ValueError):
            continue
        if uid not in cast:
            continue    # модель придумала человека или скрывшегося — не трогаем
        milestones = _milestones(item.get("milestones"), day, msg_ids)
        summary = str(item.get("summary") or "").strip()[:SUMMARY_MAX]
        if not milestones and not summary:
            continue
        if await repo.update_dossier(chat.id, uid, day=day, milestones=milestones,
                                     summary=summary):
            touched.append(cast[uid])
    if touched:
        log.info("Досье %s за %s: %s", chat.tg_id, day, ", ".join(touched))
    return touched


def _milestones(value: Any, day: date_type, msg_ids: set[int]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:MAX_MILESTONES_PER_DAY]:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:MILESTONE_MAX]
        if not text:
            continue
        msg_id = None
        for raw in item.get("msg_ids") or []:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number in msg_ids:
                msg_id = number
                break
        result.append({"day": day.isoformat(), "text": text, "msg_id": msg_id})
    return result


async def backfill(
    chat: Chat,
    today: date_type,
    *,
    days: int = BACKFILL_DAYS,
    cut: time | None = None,
    llm: Any = chat_json,
    progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Собрать досье по архиву выпусков — от старых дней к новым, вызов на день.

    Решение владельца (30.10): в характеристику — всё, что известно о человеке
    за время ведения дайджеста. Сбой одного дня не мешает остальным.
    """
    from src.digest.knowledge import day_topics

    since = today - timedelta(days=days)
    digests = sorted(
        (d for d in await repo.list_chat_digests(chat.id, limit=days + 5)
         if since <= d.day <= today),
        key=lambda d: d.day,
    )
    touched: list[str] = []
    for number, digest in enumerate(digests, start=1):
        if progress:
            progress(f"{number} из {len(digests)}: {digest.day:%d.%m}")
        if digest.id is None:
            continue
        try:
            topics = day_topics(digest, await repo.get_digest_items(digest.id))
            people = await repo.day_people(chat.id, digest.day, cut)
            touched += await update_dossiers(chat, digest.day, topics, people, llm=llm)
        except Exception:
            log.warning("Досье: день %s не разобрался", digest.day, exc_info=True)
    log.info("Досье %s по архиву: %s дней, обновлений %s", chat.tg_id, len(digests),
             len(touched))
    return touched
