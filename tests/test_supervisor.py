"""Тесты супервизора APP_ROLE=all: вместо сервисов — крошечные python-процессы."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from src import supervisor
from src.config import Settings
from src.supervisor import Service, plan_services, run_service, terminate


def settings(tmp_path: Path, **kw: object) -> Settings:
    base: dict[str, object] = dict(
        DATABASE_URL="postgresql://u:p@localhost/db",
        BOT_TOKEN="123:abc",
        OWNER_ID=1,
        WEB_SECRET_KEY="secret",
        TG_API_ID=1,
        TG_API_HASH="hash",
        TG_SESSION_STRING="session",
        TG_SESSION=str(tmp_path / "absent.session"),
    )
    base.update(kw)
    return Settings(_env_file=tmp_path / "absent.env", **base)  # type: ignore[arg-type]


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "RESTART_MIN_SEC", 0.01)
    monkeypatch.setattr(supervisor, "RESTART_MAX_SEC", 0.05)


# ------------------------------------------------------------------- план

def test_everything_configured_runs_all_three(tmp_path: Path):
    assert plan_services(settings(tmp_path)).run == ["bot", "web", "collector"]


def test_service_without_its_settings_is_skipped_not_crashlooped(tmp_path: Path):
    """Без сессии коллектор не стартует — но бот и админка должны работать."""
    plan = plan_services(settings(tmp_path, TG_SESSION_STRING="", TG_API_HASH=""))

    assert plan.run == ["bot", "web"]
    assert plan.skipped == {"collector": ["TG_API_HASH", "TG_SESSION_STRING"]}


def test_session_file_is_enough_for_collector(tmp_path: Path):
    session = tmp_path / "collector.session"
    session.write_bytes(b"")
    plan = plan_services(settings(tmp_path, TG_SESSION_STRING="", TG_SESSION=str(session)))

    assert "collector" in plan.run


def test_without_database_nothing_runs(tmp_path: Path):
    plan = plan_services(settings(tmp_path, DATABASE_URL=""))

    assert plan.run == []
    assert all("DATABASE_URL" in missing for missing in plan.skipped.values())


def test_services_can_be_chosen_explicitly(tmp_path: Path):
    plan = plan_services(settings(tmp_path, SERVICES="web, bot, bot, radio"))

    assert plan.run == ["web", "bot"], "порядок из настройки, без дублей и неизвестных"


def test_pool_is_shared_between_three_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Пулер Supabase пускает 15 клиентов: по 10 на процесс было бы 30."""
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    env = supervisor.child_env(settings(tmp_path), base={"PATH": "/bin"})

    assert env["DB_POOL_SIZE"] == str(supervisor.POOL_SIZE_PER_SERVICE)
    assert env["PATH"] == "/bin"


def test_bot_does_not_migrate_again_under_supervisor(tmp_path: Path):
    """Схему накатывает супервизор; повтор в боте столкнулся бы с запросами коллектора."""
    env = supervisor.child_env(settings(tmp_path), base={})
    child = Settings(_env_file=tmp_path / "absent.env", MIGRATE_ON_START=env["MIGRATE_ON_START"])  # type: ignore[arg-type]

    assert child.MIGRATE_ON_START is False


def test_explicit_pool_size_is_respected(tmp_path: Path):
    env = supervisor.child_env(settings(tmp_path, DB_POOL_SIZE=7), base={"DB_POOL_SIZE": "7"})

    assert env["DB_POOL_SIZE"] == "7"


# ------------------------------------------------------------ перезапуски

async def test_crashed_service_is_restarted(fast: None):
    stop = asyncio.Event()
    service = Service("collector", py("import sys; sys.exit(3)"), env={})

    task = asyncio.create_task(run_service(service, stop))
    for _ in range(200):
        if service.starts >= 3:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert service.starts >= 3


async def test_stop_interrupts_restart_pause(monkeypatch: pytest.MonkeyPatch):
    """Остановка не должна ждать пятиминутную паузу перед перезапуском."""
    monkeypatch.setattr(supervisor, "RESTART_MIN_SEC", 300.0)
    stop = asyncio.Event()
    service = Service("bot", py("pass"), env={})

    task = asyncio.create_task(run_service(service, stop))
    for _ in range(200):
        if service.starts and service.process is None:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert service.starts == 1


async def test_terminate_stops_running_services():
    stop = asyncio.Event()
    service = Service("web", py("import time; time.sleep(60)"), env={})
    task = asyncio.create_task(run_service(service, stop))
    for _ in range(200):
        if service.process is not None:
            break
        await asyncio.sleep(0.02)

    stop.set()
    await terminate([service], grace_sec=5)
    await asyncio.wait_for(task, 5)

    assert service.process is None


async def test_terminate_kills_the_stubborn_ones():
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); " \
           "print('ready', flush=True); time.sleep(60)"
    process = await asyncio.create_subprocess_exec(
        *py(code), stdout=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None
    await process.stdout.readline()   # обработчик SIGTERM уже поставлен
    service = Service("collector", [], env={}, process=process)

    await terminate([service], grace_sec=0.2)

    assert process.returncode is not None


# ---------------------------------------------------------------- целиком

async def test_supervise_migrates_first_then_runs_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast: None
):
    marker = tmp_path / "order.txt"
    script = tmp_path / "fake_mod.py"
    script.write_text(
        "import sys, time\n"
        f"open({str(marker)!r}, 'a').write(sys.argv[1] + '\\n')\n"
        "if sys.argv[1] != 'migrate':\n"
        "    time.sleep(60)\n"
    )

    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*argv: str, **kw: object):
        # `python -m src.xxx` подменяется на скрипт, который пишет, кто запустился
        module = argv[-1]
        name = next(
            (n for n, m in supervisor.SERVICE_MODULES.items() if m == module), "migrate"
        )
        return await real_exec(sys.executable, str(script), name, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    stop = asyncio.Event()
    task = asyncio.create_task(
        supervisor.supervise(settings(tmp_path), stop=stop, install_signals=False)
    )
    for _ in range(300):
        if marker.exists() and len(marker.read_text().split()) >= 4:
            break
        await asyncio.sleep(0.02)
    stop.set()
    code = await asyncio.wait_for(task, 10)

    started = marker.read_text().split()
    assert code == 0
    assert started[0] == "migrate", "сервисы стартуют только после схемы"
    assert sorted(started[1:]) == ["bot", "collector", "web"]


async def test_supervise_refuses_to_run_nothing(tmp_path: Path):
    code = await supervisor.supervise(
        settings(tmp_path, DATABASE_URL=""), stop=asyncio.Event(), install_signals=False
    )
    assert code == 1
