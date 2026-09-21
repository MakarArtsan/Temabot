"""Итоговый скор темы (TZ §4.7).

    score = w_eng·engagement + w_use·usefulness + w_spec·specificity
          + w_rel·relevance + w_nov·novelty + w_own·owner_signal

Веса и порог лежат в `chats.settings` и правятся в админке. Все слагаемые и
сырые сигналы складываются в `digest_items.features`, поэтому в админке видно,
почему тема прошла или не прошла.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.scoring.llm_rubric import Rubric

DEFAULT_WEIGHTS: dict[str, float] = {
    "w_eng": 0.25,    # вовлечённость
    "w_use": 0.30,    # применимость — главное, что нужно читателю
    "w_spec": 0.15,   # конкретика
    "w_rel": 0.15,    # попадание в профиль интересов
    "w_nov": 0.10,    # новизна
    "w_own": 0.05,    # то, что владелец уже отметил сам
}

# Штрафы: в ТЗ они названы, но не оцифрованы
DEFAULT_PENALTIES: dict[str, float] = {
    "drama": 0.35,          # склоки в дайджест не нужны
    "muted_author": 0.30,   # авторы с mute
    # Линейного веса новизны (0.10) не хватает, чтобы отсечь повтор: на прогоне
    # тема со вчерашним содержанием получала новизну 0.13 и всё равно проходила.
    # ТЗ требует обратного: повтор идёт в дайджест, только если появилось новое.
    "repeat": 0.25,
}

DEFAULT_THRESHOLD = 0.45
REPEAT_SIMILARITY = 0.85   # тот же порог, что и в novelty.py (TZ §4.7)
DEFAULT_TOP_N = 6


@dataclass(slots=True)
class Scored:
    """Оценка темы со всеми слагаемыми — их видно в админке."""

    score: float
    passed: bool
    features: dict[str, Any] = field(default_factory=dict)


def weights_from(settings: dict[str, Any] | None) -> dict[str, float]:
    """Веса из настроек чата поверх умолчаний."""
    merged = dict(DEFAULT_WEIGHTS)
    for key, value in (settings or {}).get("weights", {}).items():
        if key in merged:
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                continue
    return merged


def threshold_from(settings: dict[str, Any] | None) -> float:
    try:
        return float((settings or {}).get("threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def penalties_from(settings: dict[str, Any] | None) -> dict[str, float]:
    merged = dict(DEFAULT_PENALTIES)
    for key, value in (settings or {}).get("penalties", {}).items():
        if key in merged:
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                continue
    return merged


def compute(
    *,
    engagement: float,
    rubric: Rubric,
    novelty: float,
    normalized: dict[str, float],
    settings: dict[str, Any] | None = None,
    similarity: float = 0.0,
) -> Scored:
    """Свести три слоя в один скор."""
    weights = weights_from(settings)
    penalties = penalties_from(settings)
    threshold = threshold_from(settings)

    usefulness = rubric.usefulness / 10.0
    specificity = rubric.specificity / 10.0
    relevance = rubric.relevance / 10.0
    owner_signal = normalized.get("owner_signal", 0.0)

    base = (
        weights["w_eng"] * engagement
        + weights["w_use"] * usefulness
        + weights["w_spec"] * specificity
        + weights["w_rel"] * relevance
        + weights["w_nov"] * novelty
        + weights["w_own"] * owner_signal
    )

    penalty = 0.0
    if rubric.kind == "drama":
        penalty += penalties["drama"]
    penalty += penalties["muted_author"] * normalized.get("muted_share", 0.0)
    if similarity >= REPEAT_SIMILARITY:
        penalty += penalties["repeat"]

    score = round(max(0.0, base - penalty), 4)

    features = {
        "engagement": engagement,
        "usefulness": usefulness,
        "specificity": specificity,
        "relevance": relevance,
        "novelty": novelty,
        "owner_signal": owner_signal,
        "similarity": similarity,
        "kind": rubric.kind,
        "penalty": round(penalty, 4),
        "is_repeat": similarity >= REPEAT_SIMILARITY,
        "weights": weights,
        "threshold": threshold,
        "signals": normalized,
    }
    return Scored(score=score, passed=score >= threshold, features=features)


def select(
    scored: list[tuple[Any, Scored]], *, settings: dict[str, Any] | None = None
) -> tuple[list[Any], list[Any]]:
    """Отобрать темы выше порога, но не больше top_n (TZ §4.7).

    Возвращает (показать, отсеять). Отсеянные не выбрасываются — они нужны для
    команды `/missed` и для обучения на пропущенном.
    """
    limit = int((settings or {}).get("top_n", DEFAULT_TOP_N))
    ordered = sorted(scored, key=lambda pair: pair[1].score, reverse=True)

    shown: list[Any] = []
    missed: list[Any] = []
    for item, result in ordered:
        if result.passed and len(shown) < limit:
            shown.append(item)
        else:
            missed.append(item)
    return shown, missed
