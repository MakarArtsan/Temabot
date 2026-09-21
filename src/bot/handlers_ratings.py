"""Команды рейтингов: /top, /optout, расширенный /who (TZ §4.10)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Router, types
from aiogram.filters import Command, CommandObject

from src.config import cfg
from src.db import repo
from src.digest.render import esc_html
from src.jobs.nominations import (
    NOMINATIONS,
    find_nomination,
    ranks_of,
    top_of,
    usefulness_scale,
)

log = logging.getLogger(__name__)
router = Router(name="ratings")

PERIODS = {"day": "день", "week": "неделя", "month": "месяц"}
MEDALS = ("🥇", "🥈", "🥉")


@dataclass(slots=True)
class Period:
    key: str
    title: str
    date_from: date_type
    date_to: date_type


def resolve_period(key: str, today: date_type | None = None) -> Period:
    """Неделя — пн-вс, месяц — календарный (TZ §4.10)."""
    today = today or datetime.now(ZoneInfo(cfg.TZ)).date()
    if key == "day":
        return Period("day", f"день {today:%d.%m}", today, today)
    if key == "month":
        start = today.replace(day=1)
        return Period("month", f"месяц {today:%m.%Y}", start, today)
    start = today - timedelta(days=today.weekday())
    return Period("week", f"неделя с {start:%d.%m}", start, today)


def parse_top_args(args: str) -> tuple[str, str | None, Any]:
    """Разобрать `/top month #группа полезный` в любом порядке слов."""
    period_key = "week"
    group: str | None = None
    nomination = None

    for word in (args or "").split():
        lowered = word.lower()
        if lowered in PERIODS:
            period_key = lowered
        elif lowered in {"день", "сегодня"}:
            period_key = "day"
        elif lowered in {"неделя", "неделю"}:
            period_key = "week"
        elif lowered in {"месяц"}:
            period_key = "month"
        elif word.startswith("#"):
            group = word[1:]
        else:
            found = find_nomination(word)
            if found is not None:
                nomination = found
    return period_key, group, nomination


@router.message(Command("top"))
async def on_top(message: types.Message, command: CommandObject) -> None:
    """Рейтинги участников (TZ §4.10)."""
    period_key, group_name, nomination = parse_top_args(command.args or "")
    period = resolve_period(period_key)

    chat_id = None
    title_suffix = ""
    if group_name:
        found = await repo.find_chats(group_name)
        if not found:
            await message.answer(f"Группы «{group_name}» не знаю. Список — /groups")
            return
        chat_id = found[0].id
        title_suffix = f" · {found[0].title or found[0].tg_id}"

    rows = await repo.get_author_stats(
        chat_id, date_from=period.date_from, date_to=period.date_to
    )
    if not rows:
        await message.answer(
            f"За {period.title} статистики нет. "
            "Возможно, рейтинги ещё не считались — это делает job в 23:40."
        )
        return

    previous = await _previous_rows(chat_id, period)
    lines = [f"<b>Рейтинг · {esc_html(period.title)}{esc_html(title_suffix)}</b>", ""]

    chosen = [nomination] if nomination else NOMINATIONS
    scale = usefulness_scale(rows)

    for item in chosen:
        top = top_of(item, rows, limit=3)
        if not top:
            continue
        lines.append(f"<b>{item.title}</b>")
        for place, (row, value) in enumerate(top):
            name = esc_html(str(row.get("name") or row["tg_user_id"]))
            shown = (
                f"{scale.get(int(row['tg_user_id']), 0)} из 100"
                if item.key == "useful"
                else f"{_fmt(value)} {item.unit}".strip()
            )
            arrow = _trend(item, row, previous)
            lines.append(f"{MEDALS[place]} {name} — {shown}{arrow}")
        lines.append("")

    await message.answer("\n".join(lines).strip(), parse_mode="HTML")


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


async def _previous_rows(chat_id: int | None, period: Period) -> dict[int, dict[str, Any]]:
    """Тот же период назад — чтобы показать стрелки ↑↓ (TZ §4.10)."""
    length = (period.date_to - period.date_from).days + 1
    rows = await repo.get_author_stats(
        chat_id,
        date_from=period.date_from - timedelta(days=length),
        date_to=period.date_from - timedelta(days=1),
    )
    return {int(r["tg_user_id"]): r for r in rows}


def _trend(item: Any, row: dict[str, Any], previous: dict[int, dict[str, Any]]) -> str:
    old = previous.get(int(row["tg_user_id"]))
    if old is None:
        return ""
    now, before = item.value(row), item.value(old)
    if now > before:
        return " ↑"
    if now < before:
        return " ↓"
    return ""


@router.message(Command("optout"))
async def on_optout(message: types.Message) -> None:
    """Участник просит не показывать его в рейтингах (TZ §4.10)."""
    user = message.from_user
    if user is None:
        return
    await repo.set_hide_from_ratings(user.id, True)
    await message.answer(
        "Больше не буду показывать тебя в рейтингах. Вернуть — /optin."
    )


@router.message(Command("optin"))
async def on_optin(message: types.Message) -> None:
    user = message.from_user
    if user is None:
        return
    await repo.set_hide_from_ratings(user.id, False)
    await message.answer("Снова участвуешь в рейтингах.")


async def who_ranks(name: str, *, days: int = 30) -> list[str]:
    """Места участника в номинациях за 30 дней — дополнение к /who (§4.10)."""
    author = await repo.find_author(name)
    if author is None:
        return []

    today = datetime.now(ZoneInfo(cfg.TZ)).date()
    rows = await repo.get_author_stats(
        None, date_from=today - timedelta(days=days), date_to=today
    )
    places = ranks_of(author.tg_user_id, rows)
    if not places:
        return []

    lines = ["", f"<b>Места за {days} дней</b>"]
    for nomination, place in sorted(places, key=lambda pair: pair[1])[:5]:
        lines.append(f"{nomination.title}: {place}-е место")
    return lines


def heroes_line(rows: list[dict[str, Any]]) -> str:
    """Строка «🏅 Герои дня» для дайджеста (TZ §4.10).

    Один человек часто берёт сразу несколько номинаций. Перечислять его трижды
    подряд глупо, поэтому значки группируются по людям.
    """
    by_person: dict[str, list[str]] = {}
    order: list[str] = []

    for key in ("useful", "helper", "starter"):
        nomination = next((n for n in NOMINATIONS if n.key == key), None)
        if nomination is None:
            continue
        top = top_of(nomination, rows, limit=1)
        if not top:
            continue
        name = str(top[0][0].get("name") or top[0][0]["tg_user_id"])
        mark = nomination.title.split()[0]
        if name not in by_person:
            by_person[name] = []
            order.append(name)
        by_person[name].append(mark)

    if not order:
        return ""
    parts = [f"{' '.join(by_person[name])} {name}" for name in order]
    return "🏅 Герои дня: " + " · ".join(parts)
