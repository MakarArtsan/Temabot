"""Номинации рейтинга (TZ §4.10).

Каждая номинация — это метрика плюс правило отбора. Итог показывается как
место в тройке; сам балл полезности переводится в 0-100 как перцентиль внутри
группы за период.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MIN_MESSAGES_FOR_RATIO = 10   # «🎯 Цепляет» считается от десяти сообщений (§4.10)


@dataclass(slots=True)
class Nomination:
    key: str
    title: str
    metric: Callable[[dict[str, Any]], float]
    unit: str = ""
    positive: bool = True     # можно ли публиковать в группу (§4.10)
    eligible: Callable[[dict[str, Any]], bool] | None = None

    def value(self, row: dict[str, Any]) -> float:
        return float(self.metric(row) or 0)

    def fits(self, row: dict[str, Any]) -> bool:
        return self.eligible(row) if self.eligible else True


def _activity(row: dict[str, Any]) -> float:
    """Реплики до двух слов считаются как 0.3 (§4.10)."""
    messages = float(row.get("messages") or 0)
    short = float(row.get("short_msgs") or 0)
    return round(messages - short + short * 0.3, 2)


def _catchy(row: dict[str, Any]) -> float:
    messages = float(row.get("messages") or 0)
    return round(float(row.get("replies_got") or 0) / messages, 3) if messages else 0.0


NOMINATIONS: list[Nomination] = [
    Nomination("useful", "🧠 Самый полезный", lambda r: r.get("usefulness") or 0),
    Nomination("helper", "🛟 Помощник", lambda r: r.get("questions_answered") or 0,
               unit="ответов"),
    Nomination("starter", "🔥 Заводила", lambda r: r.get("threads_started") or 0,
               unit="тем"),
    Nomination("active", "💬 Самый активный", _activity, unit="сообщений"),
    Nomination("writer", "📜 Писатель",
               lambda r: float(r.get("words") or 0) + float(r.get("longest_msg") or 0),
               unit="слов"),
    Nomination("catchy", "🎯 Цепляет", _catchy, unit="ответов на сообщение",
               eligible=lambda r: float(r.get("messages") or 0) >= MIN_MESSAGES_FOR_RATIO),
    Nomination("loved", "❤️ Любимец публики", lambda r: r.get("reactions_got") or 0,
               unit="реакций"),
    Nomination("finder", "🔗 Добытчик", lambda r: r.get("links") or 0, unit="ссылок"),
    Nomination("radio", "🎙 Радиоведущий",
               lambda r: round(float(r.get("voice_sec") or 0) / 60, 1), unit="минут"),
    Nomination("owl", "🦉 Сова", lambda r: r.get("night_msgs") or 0, unit="ночных"),
]

BY_KEY = {n.key: n for n in NOMINATIONS}

# Поиск номинации по слову из команды: /top month полезный
ALIASES = {
    "полезный": "useful", "полезные": "useful", "польза": "useful",
    "помощник": "helper", "помощь": "helper",
    "заводила": "starter", "темы": "starter",
    "активный": "active", "активность": "active",
    "писатель": "writer", "слова": "writer",
    "цепляет": "catchy",
    "любимец": "loved", "реакции": "loved",
    "добытчик": "finder", "ссылки": "finder",
    "радиоведущий": "radio", "голосовые": "radio",
    "сова": "owl", "ночь": "owl",
}


def find_nomination(word: str) -> Nomination | None:
    cleaned = word.strip().lower()
    if cleaned in BY_KEY:
        return BY_KEY[cleaned]
    key = ALIASES.get(cleaned)
    return BY_KEY.get(key) if key else None


def top_of(
    nomination: Nomination, rows: list[dict[str, Any]], *, limit: int = 3
) -> list[tuple[dict[str, Any], float]]:
    """Тройка лидеров номинации. Нулевые результаты не показываем."""
    scored = [
        (row, nomination.value(row))
        for row in rows
        if nomination.fits(row) and nomination.value(row) > 0
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def usefulness_scale(rows: list[dict[str, Any]]) -> dict[int, int]:
    """Полезность как 0-100 — перцентиль внутри группы за период (§4.10)."""
    values = sorted(float(r.get("usefulness") or 0) for r in rows)
    if not values:
        return {}
    result: dict[int, int] = {}
    for row in rows:
        value = float(row.get("usefulness") or 0)
        below = sum(1 for item in values if item <= value)
        result[int(row["tg_user_id"])] = round(below / len(values) * 100)
    return result


def ranks_of(user_id: int, rows: list[dict[str, Any]]) -> list[tuple[Nomination, int]]:
    """Места участника во всех номинациях — для расширенного /who (§4.10)."""
    places: list[tuple[Nomination, int]] = []
    for nomination in NOMINATIONS:
        scored = [
            (int(r["tg_user_id"]), nomination.value(r))
            for r in rows
            if nomination.fits(r) and nomination.value(r) > 0
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        for place, (uid, _) in enumerate(scored, start=1):
            if uid == user_id:
                places.append((nomination, place))
                break
    return places
