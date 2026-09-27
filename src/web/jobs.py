"""Долгие действия админки в фоне: пробный прогон и ручная сборка дайджеста.

Сборка дня — это по два вызова модели на каждое обсуждение, на живом дне
несколько минут. Прокси хостинга рвёт такой запрос по таймауту, и страница
просто ничего не показывала. Теперь запрос только запускает задачу, а страница
раз в несколько секунд спрашивает, готово ли.

Задачи живут в памяти процесса админки: рестарт их теряет, но результат ручной
сборки к этому времени уже в базе, а пробный прогон легко повторить.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

KEEP_SEC = 3600          # сколько помнить завершённые задачи
TIMEOUT_SEC = 1800       # дольше получаса — считаем зависшей


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


def start(key: str, title: str, action: Callable[[], Awaitable[Any]]) -> Job:
    """Запустить задачу; если такая же ещё идёт — вернуть её."""
    _prune()
    for job in _jobs.values():
        if job.key == key and not job.done:
            return job

    job = Job(id=secrets.token_urlsafe(8), key=key, title=title)

    async def run() -> None:
        try:
            job.result = await asyncio.wait_for(action(), TIMEOUT_SEC)
        except TimeoutError:
            job.error = "не уложилось в полчаса"
        except Exception as exc:
            log.exception("Фоновая задача «%s» упала", title)
            job.error = str(exc) or exc.__class__.__name__
        finally:
            job.finished = time.monotonic()

    job.task = asyncio.create_task(run())
    _jobs[job.id] = job
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def clear() -> None:
    """Для тестов."""
    _jobs.clear()
