"""Долгие действия админки в фоне: пробный прогон и ручная сборка дайджеста.

Сборка дня — это по два вызова модели на каждое обсуждение, на живом дне
несколько минут. Прокси хостинга рвёт такой запрос по таймауту, и страница
просто ничего не показывала. Теперь запрос только запускает задачу, а страница
раз в несколько секунд спрашивает, готово ли.

Задача переживает перезапуск контейнера: пока она идёт, её описание лежит в
`state` (`job:<ключ>`), и админка при старте запускает её снова под тем же
номером — открытая страница просто дождётся результата. Ответы модели на уже
разобранные обсуждения берутся из кэша, так что продолжение идёт секунды.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.config import cfg
from src.db import repo

log = logging.getLogger(__name__)

KEEP_SEC = 3600          # сколько помнить завершённые задачи
TIMEOUT_SEC = 1800       # дольше получаса — считаем зависшей
MAX_RESUMES = 2          # если задача сама роняет процесс, не крутить её вечно
STATE_PREFIX = "job:"


@dataclass
class Job:
    id: str
    key: str                       # одинаковые задачи не запускаем дважды
    title: str
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    result: Any = None
    error: str = ""
    task: asyncio.Task[Any] | None = None
    resumed: int = 0               # сколько раз продолжали после перезапуска

    @property
    def done(self) -> bool:
        return self.finished is not None

    @property
    def elapsed(self) -> int:
        return int((self.finished or time.monotonic()) - self.started)


_jobs: dict[str, Job] = {}


def _prune() -> None:
    now = time.monotonic()
    for job_id, job in list(_jobs.items()):
        if job.finished is not None and now - job.finished > KEEP_SEC:
            _jobs.pop(job_id, None)


def job_id(key: str) -> str:
    """Номер задачи из её ключа: после перезапуска страница найдёт её по тому же адресу."""
    return re.sub(r"[^0-9A-Za-z-]+", "-", key).strip("-")


def start(
    key: str,
    title: str,
    action: Callable[[], Awaitable[Any]],
    *,
    spec: dict[str, Any] | None = None,
    resumed: int = 0,
) -> Job:
    """Запустить задачу; если такая же ещё идёт — вернуть её.

    spec — как запустить её снова (см. `resume`); без него задача живёт только в памяти.
    """
    _prune()
    running = _jobs.get(job_id(key))
    if running is not None and not running.done:
        return running

    job = Job(id=job_id(key), key=key, title=title, resumed=resumed)

    async def run() -> None:
        if spec is not None:
            await _remember(key, {**spec, "title": title, "resumed": resumed})
        try:
            job.result = await asyncio.wait_for(action(), TIMEOUT_SEC)
        except TimeoutError:
            job.error = "не уложилось в полчаса"
        except Exception as exc:
            log.exception("Фоновая задача «%s» упала", title)
            job.error = str(exc) or exc.__class__.__name__
        except asyncio.CancelledError:
            # админку останавливают (выкладка, перезапуск): отметка в базе остаётся,
            # после запуска задача продолжится
            job.error = "прервано перезапуском"
            job.finished = time.monotonic()
            raise
        job.finished = time.monotonic()
        if spec is not None:
            await _forget(key)

    job.task = asyncio.create_task(run())
    _jobs[job.id] = job
    return job


async def _remember(key: str, spec: dict[str, Any]) -> None:
    if not cfg.DATABASE_URL:
        return
    try:
        await repo.set_state(STATE_PREFIX + key, spec)
    except Exception:
        log.warning("Задача «%s» не записалась — перезапуск её потеряет", key, exc_info=True)


async def _forget(key: str) -> None:
    if not cfg.DATABASE_URL:
        return
    try:
        await repo.delete_state(STATE_PREFIX + key)
    except Exception:
        log.warning("Не удалось убрать отметку задачи «%s»", key, exc_info=True)


async def pending() -> list[tuple[str, dict[str, Any]]]:
    """Задачи, которые шли, когда процесс остановился."""
    states = await repo.get_states(STATE_PREFIX)
    return [
        (key.removeprefix(STATE_PREFIX), value)
        for key, value in states.items()
        if isinstance(value, dict)
    ]


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def clear() -> None:
    """Для тестов."""
    _jobs.clear()
