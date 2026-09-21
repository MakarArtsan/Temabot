"""Рейтинги участников (TZ §4.10).

Считается по дням, неделя и месяц — сумма по дням. Пересчёт идемпотентный:
любой день можно перегнать заново, и результат будет тот же.

Защита от накрутки:
  * подряд идущие короткие сообщения склеиваются в одно;
  * самореплаи не приносят ответов;
  * число реакций, засчитываемых одному сообщению, ограничено.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.config import cfg
from src.db import pool, repo
from src.db.models import Chat, Message

log = logging.getLogger(__name__)

# Защита от накрутки (TZ §4.10)
FLOOD_RUN = 5            # больше пяти подряд
FLOOD_WORDS = 3          # коротких сообщений
FLOOD_WINDOW_SEC = 120   # за две минуты — это одно сообщение
MAX_REACTIONS_PER_MSG = 5

SHORT_REPLY_WORDS = 2    # «ок», «спасибо» — вклад 0.3 (номинация «самый активный»)
SHORT_REPLY_WEIGHT = 0.3
NIGHT_FROM, NIGHT_TO = 0, 6

# Вклад в тред по ролям (TZ §4.10)
ROLE_WEIGHTS = {"initiator": 0.3, "key": 0.5, "answerer": 0.4}
MAX_ROLE_WEIGHT = 1.0
REACTIONS_BONUS = 0.2
REPLIES_BONUS = 0.3

_URL_RE = re.compile(r"https?://\S+")


@dataclass(slots=True)
class AuthorDay:
    """Дневная статистика участника — строка author_stats_daily."""

    chat_id: int
    tg_user_id: int
    day: date_type
    messages: int = 0
    short_msgs: int = 0
    words: int = 0
    longest_msg: int = 0
    voice_sec: int = 0
    links: int = 0
    replies_got: int = 0
    reactions_got: int = 0
    questions_answered: int = 0
    threads_started: int = 0
    night_msgs: int = 0
    usefulness: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def activity(self) -> float:
        """Для номинации «💬 Самый активный»: короткие реплики весят 0.3."""
        long_msgs = self.messages - self.short_msgs
        return round(long_msgs + self.short_msgs * SHORT_REPLY_WEIGHT, 2)


def words_in(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def merge_flood(messages: list[Message]) -> list[list[Message]]:
    """Склеить серии коротких сообщений одного автора в группы.

    Каждая группа идёт в зачёт как одно сообщение. Пять «ок» подряд — это одна
    реплика, а не пять (TZ §4.10).
    """
    groups: list[list[Message]] = []
    run: list[Message] = []

    def flush() -> None:
        nonlocal run
        if not run:
            return
        if len(run) > FLOOD_RUN:
            groups.append(run)          # вся серия — одно сообщение
        else:
            groups.extend([m] for m in run)
        run = []

    for message in sorted(messages, key=lambda m: (m.date, m.tg_msg_id)):
        short = words_in(message.content or "") <= FLOOD_WORDS and not message.media_type
        if not short:
            flush()
            groups.append([message])
            continue
        if run and (message.date - run[-1].date).total_seconds() <= FLOOD_WINDOW_SEC:
            run.append(message)
        else:
            flush()
            run = [message]
    flush()
    return groups


def collect_day(
    chat: Chat, day: date_type, messages: list[Message], *, tz: str | None = None
) -> dict[int, AuthorDay]:
    """Посчитать дневную статистику всех участников."""
    zone = ZoneInfo(tz or cfg.TZ)
    by_author: dict[int, AuthorDay] = {}
    by_id = {m.tg_msg_id: m for m in messages}

    def slot(user_id: int) -> AuthorDay:
        if user_id not in by_author:
            by_author[user_id] = AuthorDay(chat_id=chat.id, tg_user_id=user_id, day=day)
        return by_author[user_id]

    grouped: dict[int, list[Message]] = {}
    for message in messages:
        if message.tg_user_id is None:
            continue
        grouped.setdefault(message.tg_user_id, []).append(message)

    for user_id, own in grouped.items():
        stats = slot(user_id)
        for group in merge_flood(own):
            head = group[0]
            merged = len(group) > 1
            # склеенная серия идёт в зачёт как одно сообщение — и по словам тоже,
            # иначе десять «ок» подряд поднимали бы человека в номинации «Писатель»
            text = head.content or "" if merged else " ".join(
                (m.content or "") for m in group
            )
            count = words_in(text)

            stats.messages += 1
            if count <= SHORT_REPLY_WORDS and not head.media_type:
                stats.short_msgs += 1
            stats.words += count
            stats.longest_msg = max(stats.longest_msg, count)
            stats.links += len(
                _URL_RE.findall(" ".join((m.content or "") for m in group))
            )
            if head.date.astimezone(zone).hour in range(NIGHT_FROM, NIGHT_TO):
                stats.night_msgs += 1

            for message in group:
                stats.voice_sec += int((message.raw or {}).get("duration", 0) or 0)
                stats.reactions_got += min(message.reactions, MAX_REACTIONS_PER_MSG)

    # ответы: самореплаи не считаются (TZ §4.10)
    for message in messages:
        parent = by_id.get(message.reply_to) if message.reply_to else None
        if parent is None or parent.tg_user_id is None:
            continue
        if parent.tg_user_id == message.tg_user_id:
            continue
        slot(parent.tg_user_id).replies_got += 1

    return by_author


def percentile(value: float, population: list[float]) -> float:
    if not population:
        return 0.0
    below = sum(1 for item in population if item <= value)
    return below / len(population)


def apply_usefulness(
    stats: dict[int, AuthorDay], contributions: list[dict[str, Any]]
) -> None:
    """Формула полезности из §4.10.

    usefulness = Σ по тредам: score треда × вклад участника (роли суммируются,
    максимум 1.0) + 0.2·перцентиль(реакции) + 0.3·перцентиль(ответы на сообщение).
    """
    roles: dict[tuple[int, int], float] = {}
    scores: dict[int, float] = {}

    for row in contributions:
        user_id = int(row["tg_user_id"])
        thread_id = int(row["thread_id"])
        weight = ROLE_WEIGHTS.get(str(row.get("role")), 0.0)
        key = (user_id, thread_id)
        roles[key] = min(MAX_ROLE_WEIGHT, roles.get(key, 0.0) + weight)
        scores[thread_id] = float(row.get("score") or 0.0)

    for (user_id, thread_id), weight in roles.items():
        if user_id in stats:
            stats[user_id].usefulness += scores.get(thread_id, 0.0) * weight

    reactions = [float(s.reactions_got) for s in stats.values()]
    ratios = [
        (s.replies_got / s.messages) if s.messages else 0.0 for s in stats.values()
    ]
    for entry in stats.values():
        ratio = (entry.replies_got / entry.messages) if entry.messages else 0.0
        entry.usefulness += REACTIONS_BONUS * percentile(entry.reactions_got, reactions)
        entry.usefulness += REPLIES_BONUS * percentile(ratio, ratios)
        entry.usefulness = round(entry.usefulness, 4)


@dataclass(slots=True)
class RatingsResult:
    chat_tg_id: int
    day: date_type
    authors: int = 0
    rows: list[AuthorDay] = field(default_factory=list)

    def describe(self) -> str:
        return f"чат {self.chat_tg_id}, {self.day}: участников {self.authors}"


async def recalc_day(chat: Chat, day: date_type) -> RatingsResult:
    """Пересчитать день. Повторный запуск даёт тот же результат."""
    messages = await repo.get_messages_by_day(chat.id, day, include_deleted=False)
    stats = collect_day(chat, day, messages)

    contributions = await repo.get_thread_contributions(chat.id, day)
    for row in contributions:
        user_id = int(row["tg_user_id"])
        if user_id in stats and row.get("role") == "initiator" and row.get("shown"):
            stats[user_id].threads_started += 1
        if user_id in stats and row.get("role") == "answerer":
            stats[user_id].questions_answered += 1

    apply_usefulness(stats, contributions)
    await repo.replace_author_stats(chat.id, day, [s.as_row() for s in stats.values()])

    return RatingsResult(
        chat_tg_id=chat.tg_id, day=day, authors=len(stats), rows=list(stats.values())
    )


async def recalc_all(day: date_type | None = None) -> list[RatingsResult]:
    target = day or (datetime.now(ZoneInfo(cfg.TZ)).date())
    results = []
    for chat in await repo.list_chats(collect=True):
        try:
            results.append(await recalc_day(chat, target))
        except Exception:
            log.exception("Рейтинги группы %s за %s не посчитались", chat.tg_id, target)
    await repo.set_state(
        "ratings:last_run",
        {"day": target.isoformat(), "at": datetime.now(ZoneInfo(cfg.TZ)).isoformat()},
    )
    return results


async def backfill(days: int = 30, until: date_type | None = None) -> int:
    """Пересчитать историю — например, после включения рейтингов (TZ §4.10)."""
    last = until or datetime.now(ZoneInfo(cfg.TZ)).date()
    total = 0
    for offset in range(days):
        day = last - timedelta(days=offset)
        for result in await recalc_all(day):
            total += result.authors
    return total


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Пересчёт рейтингов участников")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, по умолчанию сегодня")
    parser.add_argument("--backfill", type=int, default=0, help="пересчитать N дней назад")
    args = parser.parse_args()
    logging.basicConfig(level=cfg.LOG_LEVEL)

    try:
        if args.backfill:
            total = await backfill(args.backfill)
            print(f"Пересчитано записей: {total}")
        else:
            day = date_type.fromisoformat(args.date) if args.date else None
            for result in await recalc_all(day):
                print(result.describe())
    finally:
        await pool.close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
