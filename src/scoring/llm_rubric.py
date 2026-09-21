"""Слой 2: оценка треда по рубрике дешёвой моделью (TZ §4.7).

Один вызов на тред, строго JSON. В промпт идут профиль интересов чата и до восьми
примеров из обратной связи — четыре последних 👍 и четыре последних 👎. Именно
они делают отбор «как выбрал бы владелец», а не «по числу сообщений».
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from src.db.models import Chat
from src.digest import prompts
from src.llm.client import Usage, chat_json
from src.nlp.threads import Thread

log = logging.getLogger(__name__)

KINDS = {"decision", "insight", "resource", "announcement", "question", "drama", "other"}
MAX_THREAD_CHARS = 12_000
MAX_EXAMPLES = 8

LLMCall = Callable[..., Awaitable[tuple[Any, Usage]]]


@dataclass(slots=True)
class Rubric:
    kind: str = "other"
    usefulness: float = 0.0
    specificity: float = 0.0
    relevance: float = 0.0
    takeaway: str = ""
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_empty(self) -> bool:
        return self.usefulness <= 0 and not self.takeaway


def _clamp(value: Any, low: float = 0.0, high: float = 10.0) -> float:
    """Модель охотно возвращает 11, «8/10» и строки — приводим к шкале."""
    try:
        number = float(str(value).split("/")[0].strip())
    except (TypeError, ValueError):
        return 0.0
    return max(low, min(high, number))


def format_examples(examples: list[dict[str, Any]]) -> str:
    """Примеры оценок владельца для few-shot."""
    if not examples:
        return ""
    lines = []
    for item in examples[:MAX_EXAMPLES]:
        mark = {1: "👍 полезно", -1: "👎 мимо", -2: "🔕 больше не показывать"}.get(
            int(item.get("value", 0)), "?"
        )
        title = str(item.get("title") or "").strip()
        takeaway = str(item.get("takeaway") or "").strip()
        lines.append(f"- {mark}: {title}" + (f" — {takeaway}" if takeaway else ""))
    return prompts.RUBRIC_EXAMPLES.format(examples="\n".join(lines))


async def rate_thread(
    thread: Thread,
    chat: Chat,
    *,
    examples: list[dict[str, Any]] | None = None,
    llm: LLMCall = chat_json,
) -> tuple[Rubric, Usage]:
    """Оценить тред по рубрике. Ошибка модели не роняет дайджест."""
    settings = chat.settings or {}
    profile = settings.get("interests_profile")
    profile_block = prompts.RUBRIC_PROFILE.format(profile=profile) if profile else ""

    user = prompts.RUBRIC_USER.format(
        profile=profile_block,
        examples=format_examples(examples or []),
        text=thread.text[:MAX_THREAD_CHARS],
    )

    try:
        data, usage = await llm(
            [
                {"role": "system", "content": prompts.RUBRIC_SYSTEM},
                {"role": "user", "content": user},
            ],
            purpose="score",
            chat_id=chat.id,
        )
    except Exception:
        log.exception("Рубрика не посчиталась для треда %s", thread.root_msg_id)
        raise

    if not isinstance(data, dict):
        return Rubric(), usage

    kind = str(data.get("kind", "other")).strip().lower()
    return (
        Rubric(
            kind=kind if kind in KINDS else "other",
            usefulness=_clamp(data.get("usefulness")),
            specificity=_clamp(data.get("specificity")),
            relevance=_clamp(data.get("relevance")),
            takeaway=str(data.get("takeaway") or "").strip(),
            why=str(data.get("why") or "").strip(),
        ),
        usage,
    )
