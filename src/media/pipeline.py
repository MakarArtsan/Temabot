"""Очередь обработки медиа (TZ шаг 5).

Скачивание и расшифровка идут в фоне: приём новых сообщений не должен ждать,
пока распознается трёхминутное голосовое. Одновременно обрабатывается не больше
`MEDIA_CONCURRENCY` файлов (по умолчанию 2) — ограничение сделано числом
рабочих задач, это то же самое, что семафор, но проще останавливается.

Очередь живёт только в памяти: если процесс убить, нерасшифрованные сообщения
останутся в БД с пустым transcript. Их подберёт повторный проход (TZ §4.9,
кнопка «переиндексация»), потому что в БД видно, у каких voice-сообщений
transcript пуст.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import cfg
from src.db import repo

log = logging.getLogger(__name__)

# Что имеет смысл расшифровывать
TRANSCRIBABLE = {"voice", "audio"}
# Что имеет смысл описывать мультимодальной моделью (TZ §4.1)
DESCRIBABLE = {"photo"}


@dataclass(slots=True)
class MediaJob:
    """Задание на обработку одного медиасообщения."""

    chat_id: int          # внутренний id чата
    chat_tg_id: int
    tg_msg_id: int
    media_type: str
    message: Any          # объект Telethon, у него и качаем файл


@dataclass(slots=True)
class QueueStats:
    """Для страницы «Система» в админке (TZ §4.9): очередь расшифровки."""

    queued: int = 0
    done: int = 0
    failed: int = 0
    dropped: int = 0
    skipped: int = 0
    by_telegram: int = 0    # расшифровано силами Telegram, без скачивания
    by_whisper: int = 0     # расшифровано локально

    def as_dict(self) -> dict[str, int]:
        return {
            "queued": self.queued,
            "done": self.done,
            "failed": self.failed,
            "dropped": self.dropped,
            "skipped": self.skipped,
            "by_telegram": self.by_telegram,
            "by_whisper": self.by_whisper,
        }


Transcriber = Callable[[str | Path], Awaitable[str]]
TelegramTranscriber = Callable[[Any], Awaitable[str | None]]
Describer = Callable[..., Awaitable[str]]


async def _no_description(path: str | Path, **kw: Any) -> str:
    """Заглушка: картинки не описываются, если модуль зрения не подключён."""
    return ""


class MediaQueue:
    """Фоновая обработка медиа. Ошибка одного файла не трогает остальные."""

    def __init__(
        self,
        transcriber: Transcriber,
        *,
        telegram_transcriber: TelegramTranscriber | None = None,
        describer: Describer | None = None,
        concurrency: int | None = None,
        maxsize: int | None = None,
        media_dir: Path | None = None,
        keep_files: bool | None = None,
    ) -> None:
        self.transcriber = transcriber
        self.telegram_transcriber = telegram_transcriber
        self.describer = describer or _no_description
        self.concurrency = concurrency if concurrency is not None else cfg.MEDIA_CONCURRENCY
        self.media_dir = media_dir or cfg.media_dir
        self.keep_files = keep_files if keep_files is not None else cfg.MEDIA_KEEP_FILES
        self._queue: asyncio.Queue[MediaJob] = asyncio.Queue(
            maxsize=maxsize if maxsize is not None else cfg.MEDIA_QUEUE_MAXSIZE
        )
        self._workers: list[asyncio.Task[None]] = []
        self.stats = QueueStats()

    # ------------------------------------------------------------- жизненный цикл

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(n), name=f"media-worker-{n}")
            for n in range(self.concurrency)
        ]
        log.info("Очередь медиа запущена, рабочих задач: %s", self.concurrency)

    async def stop(self, *, drain: bool = False) -> None:
        """drain=True — дождаться уже принятых заданий, иначе оборвать сразу."""
        if drain and self._workers:
            await self._queue.join()
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers = []

    # ----------------------------------------------------------------- приём

    def submit(self, job: MediaJob) -> bool:
        """Поставить в очередь, не блокируя вызывающего.

        Если очередь переполнена, задание отбрасывается с записью в лог:
        потерять расшифровку не смертельно, а вот заблокировать приём сообщений —
        смертельно, ради этого очередь и заводилась.
        """
        if job.media_type not in TRANSCRIBABLE | DESCRIBABLE:
            self.stats.skipped += 1
            return False
        if job.media_type in DESCRIBABLE and not cfg.VISION_ENABLED:
            self.stats.skipped += 1
            return False
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self.stats.dropped += 1
            log.warning(
                "Очередь медиа переполнена, пропускаю сообщение %s в чате %s",
                job.tg_msg_id, job.chat_tg_id,
            )
            return False
        self.stats.queued += 1
        return True

    # ---------------------------------------------------------------- работа

    async def _worker(self, number: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._handle(job)
                self.stats.done += 1
            except asyncio.CancelledError:
                raise  # task_done вызовет finally, второй раз нельзя
            except Exception:
                # Один битый файл не должен останавливать очередь
                self.stats.failed += 1
                log.exception(
                    "Не удалось обработать медиа %s в чате %s", job.tg_msg_id, job.chat_tg_id
                )
            finally:
                self._queue.task_done()

    async def _handle(self, job: MediaJob) -> None:
        # Сначала пробуем расшифровку силами Telegram: она не требует ни
        # скачивания файла, ни процессора, ни места на диске.
        if job.media_type in TRANSCRIBABLE and self.telegram_transcriber is not None:
            text = (await self.telegram_transcriber(job.message) or "").strip()
            if text:
                await repo.set_transcript(job.chat_id, job.tg_msg_id, text)
                self.stats.by_telegram += 1
                log.info(
                    "Telegram расшифровал сообщение %s (%s символов), файл не качали",
                    job.tg_msg_id, len(text),
                )
                return
            if cfg.ASR_PROVIDER == "telegram":
                # запасного варианта нет — на этом заканчиваем
                log.info("Telegram не расшифровал сообщение %s", job.tg_msg_id)
                return

        path = await self._download(job)
        if path is None:
            raise RuntimeError(f"Telegram не отдал файл сообщения {job.tg_msg_id}")

        await repo.set_media_path(job.chat_id, job.tg_msg_id, str(path))

        if job.media_type in DESCRIBABLE:
            text = (await self.describer(path, chat_id=job.chat_id) or "").strip()
        else:
            # расшифровщик подменяемый, на пробелы полагаться нельзя
            text = (await self.transcriber(path) or "").strip()
            if text:
                self.stats.by_whisper += 1

        if text:
            await repo.set_transcript(job.chat_id, job.tg_msg_id, text)
            log.info(
                "Расшифровано сообщение %s в чате %s (%s символов)",
                job.tg_msg_id, job.chat_tg_id, len(text),
            )
        else:
            log.info("В сообщении %s речи не нашлось", job.tg_msg_id)

        if not self.keep_files:
            # файловые операции блокирующие, в корутине им не место
            await asyncio.to_thread(self._remove, path)

    @staticmethod
    def _remove(path: Path) -> None:
        with contextlib.suppress(OSError):
            path.unlink()

    async def _download(self, job: MediaJob) -> Path | None:
        target_dir = self.media_dir / str(job.chat_tg_id)
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        result = await job.message.download_media(file=str(target_dir / str(job.tg_msg_id)))
        return Path(result) if result else None


@dataclass(slots=True)
class NullMediaQueue:
    """Заглушка для ASR_ENABLED=false и для --dry-run: принимает и забывает."""

    stats: QueueStats = field(default_factory=QueueStats)

    def submit(self, job: MediaJob) -> bool:
        self.stats.skipped += 1
        return False

    async def start(self) -> None:
        return None

    async def stop(self, *, drain: bool = False) -> None:
        return None
