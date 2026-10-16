"""База полезного: лайфхаки, решения проблем и ответы на частые вопросы.

Решение владельца (15.10, см. docs/PROGRESS.md): то, что помогает в работе, —
нейросети, оплаты, деньги, налоги — не должно тонуть в ленте. Раз в день, после
сборки дайджеста, модель смотрит рабочие темы дня (и отсеянные тоже: пять
разговоров про НПД и ИП в выпуск не влезут, а в базу должны) и нынешнюю базу и
решает, что добавить и что дополнить. Ответ проверяется: раздел, длина, номера
сообщений — только настоящие. Владелец архивирует лишнее, участники ищут по
ключевым словам на своей странице.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date as date_type
from datetime import timedelta
from typing import Any

from src.db import repo
from src.db.models import Chat, Digest
from src.digest import prompts
from src.digest.render import LIFE_KINDS, Topic
from src.llm.client import chat_json

log = logging.getLogger(__name__)

CATEGORIES = {
    "ai": "Нейросети и инструменты",
    "payments": "Оплаты и подписки",
    "money": "Деньги и заказчики",
    "taxes": "Налоги и самозанятость",
    "legal": "Договоры и права",
    "work": "Работа и процессы",
    "other": "Разное",
}
CATEGORY_MARKS = {"ai": "🤖", "payments": "💳", "money": "💰", "taxes": "🧾",
                  "legal": "📄", "work": "🛠", "other": "📌"}
BACKFILL_DAYS = 31        # «собрать за последний месяц»
PROMPT_LIMIT = 150       # сколько записей базы показывать модели
MAX_ADDS_PER_DAY = 8
TITLE_MAX = 120
BODY_MAX = 1500


def enabled(chat: Chat) -> bool:
    """Включено по умолчанию; выключается на странице «Полезное»."""
    return bool(((chat.settings or {}).get("knowledge") or {}).get("enabled", True))


def material(topics: list[Topic]) -> list[Topic]:
    """Рабочие темы дня с пересказом — откуда берётся полезное."""
    return [t for t in topics if t.kind not in LIFE_KINDS and (t.summary or t.gist)]


def _topic_line(topic: Topic) -> str:
    ids = sorted({topic.thread_id, *topic.key_msg_ids})
    parts = [f"- {topic.title}: {topic.summary or topic.gist}"]
    if topic.decision:
        parts.append(f"Итог: {topic.decision}.")
    if topic.mentions:
        parts.append(f"Упоминали: {', '.join(topic.mentions[:8])}.")
    if topic.links:
        parts.append(f"Ссылки: {', '.join(topic.links[:3])}.")
    parts.append(f"[сообщения: {', '.join(map(str, ids))}]")
    return " ".join(parts)


def _tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        tag = str(item).strip().lower()[:40]
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:10]


async def update_knowledge(
    chat: Chat, day: date_type, topics: list[Topic], *, llm: Any = chat_json
) -> list[str]:
    """Пополнить базу по рабочим темам дня. Возвращает заголовки новых записей."""
    picked = material(topics)
    if not enabled(chat) or not picked:
        return []
    existing = await repo.list_knowledge(chat.id, include_archived=True, limit=PROMPT_LIMIT)
    lines = [
        f"{r['id']}. [{r['category']}]{' [архив]' if r['archived'] else ''} {r['title']}"
        for r in existing
    ]
    system = prompts.KNOWLEDGE_SYSTEM.replace("{day}", f"{day:%d.%m.%Y}")
    data, _usage = await llm(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompts.KNOWLEDGE_USER.format(
                existing="\n".join(lines) or "пока пусто",
                day=f"{day:%d.%m.%Y}",
                topics="\n".join(_topic_line(t) for t in picked),
            )},
        ],
        purpose="knowledge",
        chat_id=chat.id,
    )
    if not isinstance(data, dict):
        return []

    msg_ids = {i for t in picked for i in (t.thread_id, *t.key_msg_ids)}
    added: list[str] = []
    for item in (data.get("add") or [])[:MAX_ADDS_PER_DAY]:
        entry = _clean(item, msg_ids)
        if entry is None:
            continue
        new_id = await repo.add_knowledge(
            chat.id, title=entry["title"], body=entry["body"], category=entry["category"],
            tags=entry["tags"], day=day,
            sources=[{"day": day.isoformat(), "msg_id": i} for i in entry["msg_ids"]],
        )
        if new_id is not None:
            added.append(entry["title"])

    # дополняем только живые записи: архив — решение владельца
    live = {int(r["id"]) for r in existing if not r["archived"]}
    for item in data.get("update") or []:
        if not isinstance(item, dict):
            continue
        try:
            knowledge_id = int(str(item.get("id")))
        except (TypeError, ValueError):
            continue
        if knowledge_id not in live:
            continue
        await repo.update_knowledge(
            knowledge_id, chat.id, day=day,
            body=str(item.get("body") or "").strip()[:BODY_MAX],
            tags=_tags(item.get("tags")),
        )
    if added:
        log.info("База полезного %s пополнилась: %s", chat.tg_id, "; ".join(added))
    return added


def _clean(item: Any, msg_ids: set[int]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()[:TITLE_MAX]
    body = str(item.get("body") or "").strip()[:BODY_MAX]
    if not title or not body:
        return None
    category = str(item.get("category") or "other").strip().lower()
    if category not in CATEGORIES:
        category = "other"
    ids: list[int] = []
    for raw in item.get("msg_ids") or []:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number in msg_ids and number not in ids:
            ids.append(number)
    return {"title": title, "body": body, "category": category,
            "tags": _tags(item.get("tags")), "msg_ids": ids[:3]}


def day_topics(digest: Digest, items: list[dict[str, Any]]) -> list[Topic]:
    """Темы прошлого дня из архива: все разобранные, включая отсеянные.

    У тем, попавших в выпуск, в архиве есть полный пересказ; у отсеянных — только
    суть одной строкой из оценки. Для базы полезного этого хватает.
    """
    from src.digest.render import DigestData

    shown: dict[int, Topic] = {}
    try:
        data = DigestData.from_dict(digest.payload) if digest.payload else None
    except (TypeError, ValueError, KeyError):
        data = None
    if data is not None:
        shown = {t.thread_id: t for t in data.topics}
    topics: list[Topic] = []
    for item in items:
        thread_id = int(item.get("thread_id") or 0)
        if thread_id in shown:
            topic = shown.pop(thread_id)
            topic.kind = str(item.get("kind") or topic.kind)
            topics.append(topic)
            continue
        features = item.get("features") or {}
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except ValueError:
                features = {}
        takeaway = str((features or {}).get("takeaway") or "").strip()
        topics.append(Topic(thread_id=thread_id, title=str(item.get("title") or ""),
                            kind=str(item.get("kind") or "other"), takeaway=takeaway))
    topics.extend(shown.values())
    return sorted(topics, key=lambda t: t.thread_id)


async def backfill(
    chat: Chat,
    today: date_type,
    *,
    days: int = BACKFILL_DAYS,
    llm: Any = chat_json,
    progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Пополнить базу по архиву выпусков за последние дни — от старых к новым.

    Решение владельца (16.10): база появилась 15.10, а полезное за прошлый месяц
    тоже должно в ней быть. Один вызов модели на день; сбой дня не мешает остальным.
    """
    since = today - timedelta(days=days)
    digests = [d for d in await repo.list_chat_digests(chat.id, limit=days + 5)
               if since <= d.day <= today]
    added: list[str] = []
    for number, digest in enumerate(sorted(digests, key=lambda d: d.day), start=1):
        if progress:
            progress(f"{number} из {len(digests)}: {digest.day:%d.%m}")
        if digest.id is None:
            continue
        try:
            items = await repo.get_digest_items(digest.id)
            added += await update_knowledge(chat, digest.day, day_topics(digest, items), llm=llm)
        except Exception:
            log.warning("База полезного: день %s не разобрался", digest.day, exc_info=True)
    log.info("База полезного %s: за %s дней добавлено %s", chat.tg_id, len(digests), len(added))
    return added
