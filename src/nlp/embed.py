"""Эмбеддинги: локальная модель или OpenAI-совместимый провайдер (TZ §1, §4.4).

У DeepSeek эндпоинта эмбеддингов нет, поэтому при EMBED_BACKEND=api нужен
отдельный провайдер — его адрес и ключ задаются отдельно от чат-модели.
Локальный bge-m3 даёт те же 1024 измерения, что и в схеме БД.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.config import cfg

log = logging.getLogger(__name__)

_model: Any = None
_client: Any = None
_lock = asyncio.Lock()


def load_local_model() -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise RuntimeError(
            "Не установлен sentence-transformers. Поставь `pip install -e '.[embed]'` "
            "или переключись на EMBED_BACKEND=api"
        ) from exc

    log.info("Загружаю модель эмбеддингов %s", cfg.EMBED_MODEL)
    return SentenceTransformer(cfg.EMBED_MODEL, cache_folder=str(cfg.DATA_DIR / "models"))


def _embed_local_sync(model: Any, texts: list[str]) -> list[list[float]]:
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [list(map(float, v)) for v in vectors]


async def _embed_local(texts: list[str]) -> list[list[float]]:
    global _model
    if _model is None:
        async with _lock:
            if _model is None:
                _model = await asyncio.to_thread(load_local_model)
    # модель синхронная и CPU-bound, в корутине она остановила бы весь процесс
    return await asyncio.to_thread(_embed_local_sync, _model, texts)


async def _embed_api(texts: list[str]) -> list[list[float]]:
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        if not cfg.embed_api_key:
            raise RuntimeError("Не задан EMBED_API_KEY (или LLM_API_KEY) — см. docs/SETUP.md")
        _client = AsyncOpenAI(
            api_key=cfg.embed_api_key, base_url=cfg.embed_base_url, timeout=60.0
        )
    response = await _client.embeddings.create(model=cfg.EMBED_MODEL, input=texts)
    return [list(item.embedding) for item in response.data]


async def embed_texts(texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
    """Тексты -> векторы. Порядок сохраняется, пустой список даёт пустой результат."""
    if not texts:
        return []

    backend = _embed_api if cfg.EMBED_BACKEND == "api" else _embed_local
    result: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start : start + batch_size]
        vectors = await backend(chunk)
        if len(vectors) != len(chunk):
            raise RuntimeError(
                f"Провайдер вернул {len(vectors)} векторов вместо {len(chunk)}"
            )
        result.extend(vectors)

    dims = {len(v) for v in result}
    if dims and dims != {cfg.EMBED_DIM}:
        raise RuntimeError(
            f"Размерность {dims} не совпадает с EMBED_DIM={cfg.EMBED_DIM}. "
            "Колонка vector(1024) в схеме рассчитана на bge-m3; смени модель или схему."
        )
    return result


async def embed_one(text: str) -> list[float]:
    vectors = await embed_texts([text])
    return vectors[0]


def to_pgvector(vector: list[float]) -> str:
    """asyncpg не знает типа vector — передаём строкой, Postgres приведёт сам."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def reset() -> None:
    """Для тестов и смены модели на лету."""
    global _model, _client
    _model = None
    _client = None
