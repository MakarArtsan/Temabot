"""Еженедельный пересчёт весов отбора (TZ §4.7).

Логистическая регрессия по признакам тем против оценок владельца. Результат —
**только предложение**: новые веса пишутся в `chats.settings.weights_proposal`,
а применяет их человек в админке, увидев «было / стало». Автоматически менять
отбор нельзя: одна неудачная неделя оценок испортила бы дайджест молча.

Минимум 30 оценок, иначе не трогаем — на меньшем регрессия выучит шум.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.config import cfg
from src.db import pool, repo
from src.scoring.score import DEFAULT_WEIGHTS

log = logging.getLogger(__name__)

MIN_FEEDBACK = 30
FEATURES = ("engagement", "usefulness", "specificity", "relevance", "novelty", "owner_signal")
FEATURE_TO_WEIGHT = {
    "engagement": "w_eng",
    "usefulness": "w_use",
    "specificity": "w_spec",
    "relevance": "w_rel",
    "novelty": "w_nov",
    "owner_signal": "w_own",
}


@dataclass(slots=True)
class RetrainResult:
    chat_id: int
    samples: int = 0
    trained: bool = False
    accuracy: float = 0.0
    baseline_accuracy: float = 0.0
    weights: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    def describe(self) -> str:
        if not self.trained:
            return f"чат {self.chat_id}: {self.reason}"
        return (
            f"чат {self.chat_id}: оценок {self.samples}, "
            f"точность {self.accuracy:.2f} против {self.baseline_accuracy:.2f} у текущих весов"
        )


def build_dataset(rows: list[dict[str, Any]]) -> tuple[list[list[float]], list[int]]:
    """Признаки и метки: 👍 — это 1, 👎 и 🔕 — 0."""
    features: list[list[float]] = []
    labels: list[int] = []
    for row in rows:
        stored = row.get("features") or {}
        vector = [float(stored.get(name, 0.0) or 0.0) for name in FEATURES]
        if not any(vector):
            continue
        features.append(vector)
        labels.append(1 if int(row.get("value", 0)) > 0 else 0)
    return features, labels


def weights_from_coefficients(coefficients: list[float]) -> dict[str, float]:
    """Коэффициенты регрессии -> веса той же шкалы, что и в скоринге.

    Регрессия даёт произвольный масштаб и знаки; нас интересуют относительные
    вклады, поэтому берём модули и нормируем на сумму исходных весов.
    """
    magnitudes = [abs(float(c)) for c in coefficients]
    total = sum(magnitudes)
    if not total:
        return dict(DEFAULT_WEIGHTS)

    scale = sum(DEFAULT_WEIGHTS.values())
    # float() обязателен: sklearn отдаёт numpy-числа, и в jsonb они попадают
    # только потому, что numpy-float наследует float. Полагаться на это не стоит.
    return {
        FEATURE_TO_WEIGHT[name]: float(round(value / total * scale, 4))
        for name, value in zip(FEATURES, magnitudes, strict=True)
    }


def score_with(weights: dict[str, float], vector: list[float]) -> float:
    return sum(
        weights.get(FEATURE_TO_WEIGHT[name], 0.0) * value
        for name, value in zip(FEATURES, vector, strict=True)
    )


def accuracy_of(weights: dict[str, float], features: list[list[float]], labels: list[int]) -> float:
    """Доля правильных предсказаний на пороге, разделяющем классы лучше всего."""
    if not features:
        return 0.0
    scores = [score_with(weights, vector) for vector in features]
    best = 0.0
    for candidate in sorted(set(scores)):
        correct = sum(
            1 for score, label in zip(scores, labels, strict=True)
            if (score >= candidate) == bool(label)
        )
        best = max(best, correct / len(labels))
    return round(best, 4)


async def retrain_chat(chat_id: int, *, min_feedback: int = MIN_FEEDBACK) -> RetrainResult:
    rows = await repo.feedback_dataset(chat_id)
    result = RetrainResult(chat_id=chat_id, samples=len(rows))

    if len(rows) < min_feedback:
        result.reason = f"оценок {len(rows)}, нужно хотя бы {min_feedback}"
        return result

    features, labels = build_dataset(rows)
    if len(set(labels)) < 2:
        result.reason = "все оценки одного знака — учиться не на чем"
        return result

    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        result.reason = "не установлен scikit-learn (pip install -e '.[ml]')"
        return result

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(features, labels)
    proposal = weights_from_coefficients(list(model.coef_[0]))

    chat = await repo.get_chat_by_id(chat_id)
    current = dict(DEFAULT_WEIGHTS)
    current.update((chat.settings or {}).get("weights", {}) if chat else {})

    result.trained = True
    result.weights = proposal
    result.accuracy = accuracy_of(proposal, features, labels)
    result.baseline_accuracy = accuracy_of(current, features, labels)

    # именно предложение: применяет человек в админке (TZ §4.7)
    await repo.update_chat_settings(
        chat_id,
        {
            "weights_proposal": {
                "weights": proposal,
                "accuracy": result.accuracy,
                "baseline_accuracy": result.baseline_accuracy,
                "samples": len(features),
                "created_at": datetime.now(UTC).isoformat(),
            }
        },
    )
    log.info("Предложены новые веса: %s", result.describe())
    return result


async def retrain_all() -> list[RetrainResult]:
    results = []
    for chat in await repo.list_chats(digest=True):
        try:
            results.append(await retrain_chat(chat.id))
        except Exception:
            log.exception("Пересчёт весов для группы %s не удался", chat.tg_id)
    return results


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Пересчёт весов отбора по оценкам")
    parser.add_argument("--chat", type=int, default=None, help="внутренний id чата")
    args = parser.parse_args()
    logging.basicConfig(level=cfg.LOG_LEVEL)

    try:
        results = [await retrain_chat(args.chat)] if args.chat else await retrain_all()
        for result in results:
            print(result.describe())
            if result.trained:
                print("  предложение:", result.weights)
    finally:
        await pool.close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
