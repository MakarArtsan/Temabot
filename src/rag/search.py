"""Гибридный поиск: pgvector + полнотекстовый, слияние через RRF (TZ §4.4).

Векторный поиск понимает смысл, но промахивается на точных словах — именах
моделей, ценах, командах. Полнотекстовый наоборот. Reciprocal Rank Fusion
объединяет два списка, не требуя приводить их оценки к общей шкале: важен
только номер места в каждом списке.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.db import repo
from src.nlp.embed import embed_one, to_pgvector

log = logging.getLogger(__name__)

VECTOR_TOP = 30
TEXT_TOP = 30
FINAL_TOP = 8
RRF_K = 60  # сглаживание: без него первое место забивает всё остальное


@dataclass(slots=True)
class Hit:
    """Найденный кусок обсуждения."""

    chat_id: int
    thread_id: int
    msg_ids: list[int]
    text: str
    score: float = 0.0
    sources: list[str] = field(default_factory=list)  # vector | fts
    date_from: Any = None
    date_to: Any = None


def reciprocal_rank_fusion(
    ranked_lists: dict[str, list[dict[str, Any]]], *, k: int = RRF_K, top: int = FINAL_TOP
) -> list[Hit]:
    """Слить несколько списков в один: вклад места n равен 1/(k+n)."""
    scores: dict[int, float] = {}
    found_in: dict[int, list[str]] = {}
    rows: dict[int, dict[str, Any]] = {}

    for source, items in ranked_lists.items():
        for position, row in enumerate(items, start=1):
            key = int(row["id"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
            found_in.setdefault(key, []).append(source)
            rows.setdefault(key, row)

    best = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:top]
    return [
        Hit(
            chat_id=int(rows[key]["chat_id"]),
            thread_id=int(rows[key]["thread_id"]),
            msg_ids=list(rows[key]["msg_ids"] or []),
            text=rows[key]["text"],
            score=score,
            sources=found_in[key],
            date_from=rows[key].get("date_from"),
            date_to=rows[key].get("date_to"),
        )
        for key, score in best
    ]


async def hybrid_search(
    query: str,
    *,
    chat_id: int | None = None,
    top: int = FINAL_TOP,
    vector_top: int = VECTOR_TOP,
    text_top: int = TEXT_TOP,
) -> list[Hit]:
    """Найти самые подходящие куски обсуждений."""
    ranked: dict[str, list[dict[str, Any]]] = {}

    try:
        vector = to_pgvector(await embed_one(query))
        ranked["vector"] = await repo.search_chunks_by_vector(
            vector, chat_id=chat_id, limit=vector_top
        )
    except Exception:
        # без эмбеддингов поиск деградирует до полнотекстового, но не отказывает
        log.warning("Векторный поиск недоступен, остаётся полнотекстовый", exc_info=True)

    ranked["fts"] = await repo.search_chunks_by_text(query, chat_id=chat_id, limit=text_top)
    return reciprocal_rank_fusion(ranked, top=top)
