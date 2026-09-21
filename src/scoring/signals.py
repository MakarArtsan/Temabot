"""Слой 1: сигналы вовлечённости без обращения к модели (TZ §4.7).

Каждый сигнал нормализуется относительно самой группы — перцентилем по истории
за 30 дней. Без этого в активной группе «популярно» будет всё, а в тихой ничего:
двадцать сообщений в треде значат разное в чате на 50 и на 5000 сообщений в день.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from src.nlp.threads import Thread

# Сигналы, которые нормализуются перцентилем. Остальные и так в диапазоне 0..1.
SCALED = ("participants", "messages", "replies", "reactions", "duration_min", "links")

MIN_HISTORY = 10   # меньше — история не показательна, нормализуем внутри дня

_URL_RE = re.compile(r"https?://\S+")
_NUMBER_RE = re.compile(r"\d")
_PRICE_RE = re.compile(
    r"\d+\s*(?:руб|₽|\$|долл|евро|€|тыс|k\b|млн)", re.IGNORECASE | re.UNICODE
)


@dataclass(slots=True)
class ThreadSignals:
    """Сырые сигналы треда. Именно они попадают в `digest_items.features`."""

    participants: int = 0
    messages: int = 0
    replies: int = 0
    reactions: int = 0
    duration_min: float = 0.0
    links: int = 0
    author_weight: float = 1.0
    has_numbers: bool = False
    has_price: bool = False
    owner_signal: float = 0.0
    answered: bool = False
    has_question: bool = False
    muted_share: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect_signals(
    thread: Thread,
    *,
    owner_id: int = 0,
    author_weights: dict[int, float] | None = None,
    muted_authors: set[int] | None = None,
) -> ThreadSignals:
    """Посчитать сигналы треда. Ни сети, ни БД — всё из самих сообщений."""
    weights = author_weights or {}
    muted = muted_authors or set()
    participants = thread.participants

    text = "\n".join(m.content or "" for m in thread.messages)
    replies = sum(1 for m in thread.messages if m.reply_to)
    reactions = sum(m.reactions for m in thread.messages)

    owner_msgs = sum(1 for m in thread.messages if m.tg_user_id == owner_id)
    pinned = sum(1 for m in thread.messages if m.is_pinned_by_me)
    copied = sum(m.copy_count for m in thread.messages)
    # копирование и пин — самый честный сигнал интереса владельца: он это уже выбрал
    owner_signal = min(1.0, 0.4 * min(owner_msgs, 2) + 0.5 * bool(pinned) + 0.3 * bool(copied))

    answered = False
    if thread.has_question:
        # вопрос считается закрытым, если после него кто-то другой ответил в треде
        for index, message in enumerate(thread.messages):
            if "?" in (message.content or ""):
                answered = any(
                    later.tg_user_id != message.tg_user_id
                    for later in thread.messages[index + 1 :]
                )
                if answered:
                    break

    return ThreadSignals(
        participants=len(participants),
        messages=len(thread.messages),
        replies=replies,
        reactions=reactions,
        duration_min=round(thread.duration_min, 1),
        links=len(_URL_RE.findall(text)),
        author_weight=(
            sum(weights.get(uid, 1.0) for uid in participants) / len(participants)
            if participants
            else 1.0
        ),
        has_numbers=bool(_NUMBER_RE.search(text)),
        has_price=bool(_PRICE_RE.search(text)),
        owner_signal=round(owner_signal, 2),
        answered=answered,
        has_question=thread.has_question,
        muted_share=(
            round(len(participants & muted) / len(participants), 2) if participants else 0.0
        ),
    )


def percentile(value: float, history: list[float]) -> float:
    """Доля значений истории, которые не больше данного. 0..1."""
    if not history:
        return 0.5   # нет истории — считаем средним, чтобы не наказывать и не хвалить
    below = sum(1 for item in history if item <= value)
    return round(below / len(history), 4)


def normalize(
    signals: ThreadSignals,
    history: dict[str, list[float]],
    *,
    peers: list[ThreadSignals] | None = None,
) -> dict[str, float]:
    """Привести сигналы к 0..1 относительно группы (TZ §4.7).

    Пока истории мало, нормализуем внутри сегодняшнего дня: это хуже, чем месяц
    наблюдений, но честнее, чем абсолютные числа.
    """
    raw = signals.as_dict()
    result: dict[str, float] = {}

    for name in SCALED:
        values = history.get(name) or []
        if len(values) < MIN_HISTORY and peers:
            values = [getattr(p, name) for p in peers]
        result[name] = percentile(float(raw[name]), [float(v) for v in values])

    # эти сигналы уже осмысленны сами по себе
    result["author_weight"] = min(1.0, float(raw["author_weight"]) / 2.0)
    result["owner_signal"] = float(raw["owner_signal"])
    result["has_numbers"] = 1.0 if raw["has_numbers"] else 0.0
    result["has_price"] = 1.0 if raw["has_price"] else 0.0
    result["answered"] = 1.0 if raw["answered"] else 0.0
    result["unanswered_question"] = (
        1.0 if raw["has_question"] and not raw["answered"] else 0.0
    )
    result["muted_share"] = float(raw["muted_share"])
    return result


def engagement(normalized: dict[str, float]) -> float:
    """Свёртка сигналов вовлечённости в одно число 0..1."""
    parts = {
        "participants": 0.25,
        "replies": 0.2,
        "reactions": 0.15,
        "messages": 0.1,
        "duration_min": 0.1,
        "links": 0.1,
        "author_weight": 0.1,
    }
    total = sum(normalized.get(name, 0.0) * weight for name, weight in parts.items())
    return round(min(1.0, total), 4)
