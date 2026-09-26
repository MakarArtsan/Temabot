"""Человеческие подписи в админке: состояние процессов и названия настроек."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.scoring.score import DEFAULT_PENALTIES, DEFAULT_WEIGHTS
from src.web import labels

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _by_title(states: dict) -> dict[str, labels.ProcessState]:
    return {p.title: p for p in labels.describe_states(states, NOW)}


def test_every_setting_has_a_human_name():
    """Ни один ключ вроде w_eng не должен попасть на экран без названия."""
    assert {s.key for s in labels.WEIGHTS} == set(DEFAULT_WEIGHTS)
    assert {s.key for s in labels.PENALTIES} == set(DEFAULT_PENALTIES)
    for setting in (*labels.WEIGHTS, *labels.PENALTIES):
        assert setting.title and setting.hint and "_" not in setting.title


def test_alive_and_silent_processes():
    states = {
        "collector:heartbeat": {"at": (NOW - timedelta(seconds=30)).isoformat(), "saved": 12},
        "bot:heartbeat": {"at": (NOW - timedelta(minutes=20)).isoformat()},
    }
    got = _by_title(states)

    assert (got["Сборщик сообщений"].status, got["Сборщик сообщений"].label) == ("ok", "работает")
    assert "сохранено с запуска: 12" in got["Сборщик сообщений"].detail
    assert (got["Бот"].status, got["Бот"].label) == ("bad", "молчит")


def test_never_started_process():
    got = _by_title({})
    assert got["Бот"].label == "не запущен" and got["Бот"].at is None


def test_finished_jobs_say_done():
    states = {
        "digest:last_run": {"day": "2026-09-25", "sent": 1,
                            "at": (NOW - timedelta(hours=12)).isoformat()},
        "backfill:-100111": {"done": False, "updated_at": NOW.isoformat()},
        "schema_applied_at": f'"{(NOW - timedelta(minutes=5)).isoformat()}"',
    }
    got = _by_title(states)

    run = got["Последняя сборка дайджестов"]
    assert run.label == "готово" and "за 25.09" in run.detail and "отправлено: 1" in run.detail
    assert got["Загрузка истории"].label == "идёт"
    assert got["Схема базы"].label == "готово" and "5 мин назад" in got["Схема базы"].detail


def test_ago():
    assert labels.ago(NOW - timedelta(seconds=10), NOW) == "только что"
    assert labels.ago(NOW - timedelta(minutes=7), NOW) == "7 мин назад"
    assert labels.ago(NOW - timedelta(hours=3), NOW) == "3 ч назад"
    assert labels.ago(NOW - timedelta(days=1, hours=1), NOW).startswith("вчера в")
    assert labels.ago(None, NOW) == "нет данных"


def test_member_list_freshness_is_visible():
    fresh = {"at": (NOW - timedelta(hours=2)).isoformat(), "count": 124, "complete": True}
    stale = {"at": (NOW - timedelta(days=3)).isoformat(), "count": 90, "complete": False}

    got = labels.describe_states({"members:1": fresh}, NOW)
    row = next(p for p in got if p.title == "Список участников группы")
    assert row.label == "свежий" and "124 чел." in row.detail

    got = labels.describe_states({"members:1": stale}, NOW)
    row = next(p for p in got if p.title == "Список участников группы")
    assert row.label == "устарел" and "не всех" in row.detail
