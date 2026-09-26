"""Человеческие названия настроек и служебных полей для страниц админки.

Ключи вроде `w_eng` или `muted_author` живут в базе и коде, а на экран идут
названия отсюда — одно место, чтобы подписи на разных страницах не разъезжались.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    title: str
    hint: str


# Порядок — от главного к второстепенному: так их и читают
WEIGHTS: tuple[Setting, ...] = (
    Setting(
        "w_use", "Польза",
        "Можно ли это применить: совет, решение, рабочий способ. Главное для читателя.",
    ),
    Setting(
        "w_eng", "Живое обсуждение",
        "Сколько людей включилось, сколько было ответов и реакций — по меркам этой группы.",
    ),
    Setting(
        "w_spec", "Конкретика",
        "Цифры, названия, цены, шаги — вместо общих слов.",
    ),
    Setting(
        "w_rel", "Совпадение с интересами",
        "Насколько тема подходит под описание интересов выше. Без описания почти не работает.",
    ),
    Setting(
        "w_nov", "Новизна",
        "Тема не повторяет то, что уже было в дайджестах за последнюю неделю.",
    ),
    Setting(
        "w_own", "Ваше участие",
        "Вы сами писали в этой теме или сохраняли её сообщения через бота.",
    ),
)

PENALTIES: tuple[Setting, ...] = (
    Setting(
        "drama", "Ссоры и перепалки",
        "Сколько отнять у темы, если модель узнала в ней спор на эмоциях.",
    ),
    Setting(
        "muted_author", "Приглушённые участники",
        "Сколько отнять, если в теме пишут те, кого вы приглушили на странице «Участники».",
    ),
    Setting(
        "repeat", "Повтор без нового",
        "Сколько отнять у темы, которая почти повторяет недавнюю и не добавляет ничего нового.",
    ),
)

# Что модель думает о теме (TZ §4.7) — это видно в разборе дайджеста
KINDS: dict[str, str] = {
    "decision": "Решение",
    "insight": "Вывод",
    "resource": "Полезная ссылка",
    "announcement": "Анонс",
    "question": "Вопрос",
    "drama": "Спор",
    "other": "Обсуждение",
}

# Слагаемые оценки темы в разборе дайджеста
FEATURES: tuple[tuple[str, str], ...] = (
    ("usefulness", "польза"),
    ("engagement", "живость"),
    ("specificity", "конкретика"),
    ("relevance", "по интересам"),
    ("novelty", "новизна"),
    ("owner_signal", "ваше участие"),
)

COPIER: dict[str, str] = {"allow": "работает", "ask": "спросить меня", "deny": "выключен"}

# Режимы публикации в группу подписаны в src/digest/publish.py (PUBLISH_LABELS)

PORTAL_MODES = ("off", "digests", "all")
PORTAL: dict[str, str] = {
    "off": "закрыта",
    "digests": "дайджесты",
    "all": "дайджесты и рейтинги",
}


# --------------------------------------------------------- состояние процессов

@dataclass(frozen=True, slots=True)
class ProcessState:
    title: str
    detail: str
    at: datetime | None
    status: str          # ok | warn | bad | idle — цвет точки на значке
    label: str           # подпись значка: цвет никогда не единственный носитель смысла


# Сколько может молчать отметка, прежде чем это тревожно (в минутах)
_HEARTBEATS: dict[str, tuple[str, int]] = {
    "collector:heartbeat": ("Сборщик сообщений", 5),
    "bot:heartbeat": ("Бот", 5),
}
_RUNS: dict[str, str] = {
    "digest:last_run": "Последняя сборка дайджестов",
    "ratings:last_run": "Последний пересчёт рейтингов",
    "rag:last_index": "Последняя индексация для поиска",
}


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def ago(moment: datetime | None, now: datetime) -> str:
    """«3 мин назад», «вчера в 23:30» — для отметок процессов."""
    if moment is None:
        return "нет данных"
    seconds = (now - moment).total_seconds()
    if seconds < 90:
        return "только что"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин назад"
    if seconds < 6 * 3600:
        return f"{int(seconds // 3600)} ч назад"
    local = moment.astimezone(now.tzinfo) if now.tzinfo else moment
    days = (now.date() - local.date()).days
    if days == 0:
        return f"сегодня в {local:%H:%M}"
    if days == 1:
        return f"вчера в {local:%H:%M}"
    return f"{local:%d.%m} в {local:%H:%M}"


def _day(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%d.%m")
    except (TypeError, ValueError):
        return str(value)


def describe_states(states: dict[str, Any], now: datetime) -> list[ProcessState]:
    """Отметки процессов из таблицы state — по-человечески и со статусом."""
    result: list[ProcessState] = []

    for key, (title, limit_min) in _HEARTBEATS.items():
        value = states.get(key) or {}
        at = _parse_time(value.get("at")) if isinstance(value, dict) else None
        if at is None:
            result.append(ProcessState(title, "ещё не запускался", None, "idle", "не запущен"))
            continue
        late = (now - at).total_seconds() > limit_min * 60
        detail = f"последний отклик {ago(at, now)}"
        if key == "collector:heartbeat" and isinstance(value, dict):
            saved = value.get("saved")
            media = value.get("media") or {}
            if saved is not None:
                detail += f" · сохранено с запуска: {saved}"
            if isinstance(media, dict) and media.get("done"):
                detail += f" · расшифровано: {media['done']}"
        status, label = ("bad", "молчит") if late else ("ok", "работает")
        result.append(ProcessState(title, detail, at, status, label))

    for key, title in _RUNS.items():
        value = states.get(key)
        if not isinstance(value, dict):
            continue
        at = _parse_time(value.get("at"))
        parts = [ago(at, now)]
        if value.get("day"):
            parts.append(f"за {_day(value['day'])}")
        if "sent" in value:
            parts.append(f"отправлено: {value['sent']}")
        if "chunks" in value:
            parts.append(f"фрагментов: {value['chunks']}")
        result.append(ProcessState(title, " · ".join(parts), at, "ok", "готово"))

    applied = states.get("schema_applied_at")
    if applied:
        # строка ISO; в старых записях — ещё и в кавычках JSON
        at = _parse_time(str(applied).strip('"'))
        result.append(ProcessState("Схема базы", f"обновлена {ago(at, now)}", at, "ok", "готово"))

    # список участников для страницы участников: доверяем ему не дольше суток
    for _key, value in sorted((k, v) for k, v in states.items() if k.startswith("members:")):
        if not isinstance(value, dict):
            continue
        at = _parse_time(value.get("at"))
        stale = at is None or (now - at).total_seconds() > 24 * 3600
        detail = f"{value.get('count', 0)} чел. · сверен {ago(at, now)}"
        if not value.get("complete", True):
            detail += " · Telegram отдал не всех"
        status, label = ("warn", "устарел") if stale else ("ok", "свежий")
        result.append(ProcessState("Список участников группы", detail, at, status, label))

    backfills = {k: v for k, v in states.items() if k.startswith("backfill:")}
    for _key, value in sorted(backfills.items()):
        if not isinstance(value, dict):
            continue
        at = _parse_time(value.get("updated_at"))
        done = bool(value.get("done"))
        detail = ("история загружена" if done else "история загружается") + f" · {ago(at, now)}"
        result.append(ProcessState(
            "Загрузка истории", detail, at, "ok" if done else "warn", "готово" if done else "идёт"
        ))

    return result
