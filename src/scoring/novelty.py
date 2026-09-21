"""Слой 3: новизна темы (TZ §4.7).

Эмбеддинг `заголовок + вывод` сравнивается с темами последних семи дней.
Косинус выше 0.85 означает «об этом уже было»: тема идёт в дайджест только
если в ней появилось что-то новое.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from src.nlp.embed import embed_one

log = logging.getLogger(__name__)

SAME_TOPIC = 0.85   # выше этого — считаем повтором (TZ §4.7)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def novelty_from_similarity(similarity: float) -> float:
    """Похожесть -> новизна 0..1.

    До порога новизна почти не падает: слегка похожие темы — это нормально.
    После порога обрыв резкий, иначе повторы просачивались бы в дайджест
    каждый день (TZ §4.7).
    """
    if similarity <= 0:
        return 1.0
    if similarity < SAME_TOPIC:
        return round(1.0 - 0.3 * (similarity / SAME_TOPIC), 4)
    # 0.85 -> 0.7, 1.0 -> 0.0
    overshoot = (similarity - SAME_TOPIC) / (1.0 - SAME_TOPIC)
    return round(max(0.0, 0.7 * (1.0 - overshoot)), 4)


async def find_similar(
    title: str, recent: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, float]:
    """Найти недавнюю тему, похожую по заголовку.

    Считаем именно по заголовку, а не по выводу: вывод выдаёт рубрика, а знать
    про повтор нужно до её вызова — чтобы спросить «что нового» (TZ §4.7).
    """
    if not title or not recent:
        return None, 0.0
    try:
        embedding = await embed_one(title)
    except Exception:
        log.warning("Похожие темы не найдены: эмбеддинги недоступны", exc_info=True)
        return None, 0.0

    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in recent:
        vector = item.get("embedding") or []
        score = cosine(embedding, vector)
        if score > best_score:
            best, best_score = item, score
    if best is None or best_score < SAME_TOPIC:
        return None, round(best_score, 4)
    return best, round(best_score, 4)


async def score_novelty(
    title: str, takeaway: str, recent: list[list[float]]
) -> tuple[float, list[float] | None, float]:
    """Вернуть (новизна, эмбеддинг темы, максимальная похожесть).

    Если эмбеддинги недоступны, новизна считается нейтральной: лучше показать
    тему лишний раз, чем потерять её из-за сбоя модели.
    """
    text = f"{title}. {takeaway}".strip()
    if not text:
        return 1.0, None, 0.0

    try:
        embedding = await embed_one(text)
    except Exception:
        log.warning("Новизна не посчитана: эмбеддинги недоступны", exc_info=True)
        return 0.8, None, 0.0

    if not recent:
        return 1.0, embedding, 0.0

    similarity = max(cosine(embedding, other) for other in recent)
    return novelty_from_similarity(similarity), embedding, round(similarity, 4)
