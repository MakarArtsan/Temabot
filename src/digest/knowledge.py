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

import logging
from datetime import date as date_type
from typing import Any

from src.db import repo
from src.db.models import Chat
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
