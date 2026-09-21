"""Расшифровка голосовых через faster-whisper (TZ §4.1, шаг 5).

faster-whisper — синхронная и CPU-bound библиотека: вызывать её прямо в корутине
нельзя, иначе на время расшифровки встанет весь приём сообщений. Поэтому работа
уходит в поток через asyncio.to_thread.

Модель грузится лениво и один раз: `small` в int8 — это около 1 ГБ на диске и
пара гигабайт ОЗУ, держать её в памяти впустую незачем.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.config import cfg

log = logging.getLogger(__name__)

_model: Any = None
_model_lock = asyncio.Lock()


def load_model() -> Any:
    """Синхронная загрузка модели. faster-whisper — необязательная зависимость."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise RuntimeError(
            "Не установлен faster-whisper. Поставь `pip install -e '.[media]'` "
            "или выключи расшифровку: ASR_ENABLED=false"
        ) from exc

    log.info(
        "Загружаю whisper %s (%s, %s)",
        cfg.WHISPER_MODEL, cfg.WHISPER_DEVICE, cfg.WHISPER_COMPUTE_TYPE,
    )
    return WhisperModel(
        cfg.WHISPER_MODEL,
        device=cfg.WHISPER_DEVICE,
        compute_type=cfg.WHISPER_COMPUTE_TYPE,
        cpu_threads=cfg.WHISPER_CPU_THREADS,
        download_root=str(cfg.DATA_DIR / "models"),
    )


async def get_model() -> Any:
    """Модель на весь процесс. Первая загрузка идёт в потоке — она долгая."""
    global _model
    if _model is not None:
        return _model
    async with _model_lock:
        if _model is None:
            _model = await asyncio.to_thread(load_model)
    return _model


def _transcribe_sync(model: Any, path: str) -> str:
    """Собственно расшифровка. Выполняется в отдельном потоке."""
    segments, _info = model.transcribe(
        path,
        language=cfg.WHISPER_LANGUAGE or None,
        vad_filter=True,  # режет тишину: короче аудио — быстрее и чище текст
        beam_size=cfg.WHISPER_BEAM_SIZE,
    )
    # segments — ленивый генератор, расшифровка идёт именно здесь
    return " ".join(segment.text.strip() for segment in segments).strip()


async def transcribe(path: str | Path) -> str:
    """Путь к аудио -> текст. Пустая строка, если речи не нашлось."""
    model = await get_model()
    text = await asyncio.to_thread(_transcribe_sync, model, str(path))
    log.debug("Расшифровано %s: %s символов", path, len(text))
    return text


def reset_model() -> None:
    """Для тестов и смены модели на лету."""
    global _model
    _model = None
