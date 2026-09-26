"""Одно приложение вместо трёх: bot, web и collector в одном контейнере.

`APP_ROLE=all` (по умолчанию). На Amvera это один проект: одни переменные,
один деплой, один адрес. Внутри — маленький супервизор:

1. сразу запускает web — порт 80 отвечает через секунды, что бы ни было с базой;
2. накатывает схему БД и запускает bot и collector отдельными процессами;
3. упавший процесс перезапускает с растущей паузой, остальные при этом работают;
4. по SIGTERM (остановка или обновление проекта) аккуратно гасит всех.

Процессы, а не задачи в одном event loop, — ради изоляции: протухшая сессия
коллектора или сбой бота не должны ронять админку, через которую их чинят.
Цена — около 100 МБ памяти сверху по сравнению с одним процессом.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Settings

log = logging.getLogger("supervisor")

SERVICE_MODULES = {
    "bot": "src.bot.main",
    "web": "src.web.app",
    "collector": "src.collector",
}
MIGRATE_MODULE = "src.db.migrate"

RESTART_MIN_SEC = 5.0
RESTART_MAX_SEC = 300.0
STABLE_AFTER_SEC = 600.0    # проработал 10 минут — пауза перед перезапуском сбрасывается
STOP_TIMEOUT_SEC = 15.0
IDLE_REPORT_SEC = 300.0     # как часто напоминать в логе, почему ничего не запущено
# Пулер Supabase в режиме сессий пускает не больше 15 клиентов на всю базу,
# а процессов теперь три: делим лимит, если владелец не задал размер сам.
POOL_SIZE_PER_SERVICE = 4


@dataclass(slots=True)
class Service:
    name: str
    argv: list[str]
    env: dict[str, str]
    starts: int = 0
    process: asyncio.subprocess.Process | None = None


@dataclass(slots=True)
class Plan:
    run: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)


def missing_settings(name: str, settings: Settings) -> list[str]:
    """Без чего сервис точно не стартует — те же проверки, что он делает сам.

    Такой сервис не запускаем вовсе: иначе он падал бы и перезапускался
    бесконечно, забивая лог одной и той же ошибкой.
    """
    missing = [] if settings.DATABASE_URL else ["DATABASE_URL"]
    if name == "bot":
        missing += [k for k in ("BOT_TOKEN", "OWNER_ID") if not getattr(settings, k)]
    elif name == "web":
        missing += [k for k in ("WEB_SECRET_KEY",) if not getattr(settings, k)]
    elif name == "collector":
        missing += [k for k in ("TG_API_ID", "TG_API_HASH") if not getattr(settings, k)]
        if not settings.TG_SESSION_STRING and not Path(settings.TG_SESSION).exists():
            missing.append("TG_SESSION_STRING")
    return missing


def plan_services(settings: Settings) -> Plan:
    plan = Plan()
    for raw in settings.SERVICES.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in SERVICE_MODULES:
            log.warning("Неизвестный сервис в SERVICES: %s — пропускаю", name)
            continue
        if name in plan.run or name in plan.skipped:
            continue
        missing = missing_settings(name, settings)
        if missing:
            plan.skipped[name] = missing
        else:
            plan.run.append(name)
    return plan


def child_env(settings: Settings, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    if "DB_POOL_SIZE" not in settings.model_fields_set:
        env["DB_POOL_SIZE"] = str(POOL_SIZE_PER_SERVICE)
    # схему уже накатил супервизор: повтор в боте взял бы блокировки таблиц
    # как раз тогда, когда коллектор делает первые запросы
    env["MIGRATE_ON_START"] = "false"
    return env


async def _wait_or_stop(stop: asyncio.Event, delay: float) -> None:
    """Пауза, которая обрывается сразу, как только пришла остановка."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)


async def migrate(env: dict[str, str], stop: asyncio.Event) -> bool:
    """Накатить схему до старта сервисов. База может проснуться не сразу — повторяем."""
    delay = RESTART_MIN_SEC
    while not stop.is_set():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", MIGRATE_MODULE, env=env
        )
        if await process.wait() == 0:
            return True
        log.error("Схема БД не применилась, повтор через %.0f с", delay)
        await _wait_or_stop(stop, delay)
        delay = min(delay * 2, 60.0)
    return False


async def run_service(
    service: Service,
    stop: asyncio.Event,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Держать сервис запущенным, пока не пришла остановка."""
    delay = RESTART_MIN_SEC
    while not stop.is_set():
        started = clock()
        process = await asyncio.create_subprocess_exec(*service.argv, env=service.env)
        service.process = process
        service.starts += 1
        if stop.is_set():
            # остановка пришла, пока процесс создавался: terminate() его уже не видел
            process.terminate()
        log.info("%s запущен (pid %s)", service.name, process.pid)

        code = await process.wait()
        service.process = None
        if stop.is_set():
            return

        if clock() - started >= STABLE_AFTER_SEC:
            delay = RESTART_MIN_SEC
        log.error(
            "%s завершился с кодом %s — перезапуск через %.0f с", service.name, code, delay
        )
        await _wait_or_stop(stop, delay)
        delay = min(delay * 2, RESTART_MAX_SEC)


async def terminate(services: list[Service], grace_sec: float = STOP_TIMEOUT_SEC) -> None:
    """SIGTERM всем, а кто не успел закрыться за `grace_sec` — SIGKILL."""
    running = [
        s.process for s in services if s.process is not None and s.process.returncode is None
    ]
    for process in running:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(asyncio.gather(*(p.wait() for p in running)), grace_sec)
    except TimeoutError:
        for process in running:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        await asyncio.gather(*(p.wait() for p in running))


async def _idle(stop: asyncio.Event, reason: str) -> None:
    """Не завершаться, а ждать и напоминать о причине.

    Если процесс контейнера сразу выходит, Amvera перезапускает его по кругу и
    показывает «Ошибка развертывания» — а короткий лог при этом часто теряется.
    Живой контейнер с понятной строкой в логе чинится гораздо быстрее.
    """
    while not stop.is_set():
        log.error("%s", reason)
        await _wait_or_stop(stop, IDLE_REPORT_SEC)


async def supervise(
    settings: Settings | None = None,
    *,
    stop: asyncio.Event | None = None,
    install_signals: bool = True,
) -> int:
    if settings is None:
        from src.config import cfg

        settings = cfg

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT) if install_signals else ()
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)

    try:
        plan = plan_services(settings)
        for name, missing in plan.skipped.items():
            log.warning(
                "%s не запущен: не заданы %s. Добавь переменные и перезапусти приложение.",
                name,
                ", ".join(missing),
            )
        if not plan.run:
            await _idle(stop, "Запускать нечего — проверь переменные окружения (docs/SETUP.md)")
            return 1

        env = child_env(settings)
        services = {
            name: Service(
                name=name,
                argv=[sys.executable, "-m", SERVICE_MODULES[name]],
                env={**env, "APP_ROLE": name},
            )
            for name in plan.run
        }
        tasks: list[asyncio.Task[None]] = []

        # Админка — первой: она отвечает на /healthz и не ждёт базу, поэтому
        # Amvera видит живой порт, даже пока схема накатывается или база недоступна
        if "web" in services:
            log.info("Запускаю: web")
            tasks.append(asyncio.create_task(run_service(services["web"], stop)))

        # импорт здесь: src.db.pool читает настройки при импорте, а main() должен
        # успеть сам сообщить об их ошибках
        from src.db.pool import dsn_hint, dsn_problem

        rest = [name for name in plan.run if name != "web"]
        problem = dsn_problem(settings.DATABASE_URL)
        hint = dsn_hint(settings.DATABASE_URL)
        if hint:
            log.warning("%s", hint)
        if rest and problem:
            # повторять подключение бессмысленно: строка сломана, нужен человек
            await _idle(stop, f"bot и collector не запущены. {problem}")
        elif rest and await migrate(env, stop):
            log.info("Запускаю: %s", ", ".join(rest))
            tasks += [asyncio.create_task(run_service(services[n], stop)) for n in rest]

        await stop.wait()
        log.info("Останавливаю сервисы")
        await terminate(list(services.values()))
        await asyncio.gather(*tasks, return_exceptions=True)
        return 0
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


def _config_problems() -> str | None:
    """Прочитать настройки заранее и описать ошибки по-человечески.

    Значения в сообщение не попадают: среди них токены и пароли.
    """
    from pydantic import ValidationError

    try:
        from src.config import get_settings

        get_settings()
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        ]
        return "Переменные окружения с неверным значением:\n" + "\n".join(lines)
    return None


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    problems = _config_problems()
    if problems:
        async def report() -> None:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            await _idle(stop, problems)

        asyncio.run(report())
        sys.exit(1)
    sys.exit(asyncio.run(supervise()))


if __name__ == "__main__":
    main()
