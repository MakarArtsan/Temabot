"""Тесты рейтингов участников (TZ §4.10, шаг 12.5)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from src.bot import handlers_ratings as ratings_bot
from src.db.models import Chat, Message
from src.jobs import ratings as ratings_job
from src.jobs.nominations import (
    BY_KEY,
    find_nomination,
    ranks_of,
    top_of,
    usefulness_scale,
)

VASYA, PETYA, MASHA = 101, 102, 103
DAY = date(2026, 9, 20)
START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
CHAT = Chat(id=1, tg_id=-1002354231333, title="Группа")


def msg(tg_msg_id: int, *, user: int = VASYA, text: str = "нормальное сообщение тут",
        minutes: float = 0, reply_to: int | None = None, **kw: Any) -> Message:
    return Message(
        chat_id=1, tg_msg_id=tg_msg_id, tg_user_id=user, author_name=f"user{user}",
        text=text, reply_to=reply_to,
        date=START + timedelta(minutes=minutes), **kw,
    )


# ======================================================= защита от накрутки

def test_flood_of_ten_short_messages_counts_as_one():
    """Прямое требование ТЗ: флуд из десяти коротких сообщений — одно."""
    flood = [msg(n, text="ок", minutes=n * 0.1) for n in range(1, 11)]
    stats = ratings_job.collect_day(CHAT, DAY, flood)

    assert stats[VASYA].messages == 1


def test_five_short_messages_are_not_flood():
    """Порог — «больше пяти», ровно пять считаются по отдельности."""
    few = [msg(n, text="ок", minutes=n * 0.1) for n in range(1, 6)]
    assert ratings_job.collect_day(CHAT, DAY, few)[VASYA].messages == 5


def test_short_messages_apart_in_time_are_not_flood():
    """Две минуты тишины — это уже не серия."""
    spread = [msg(1, text="ок"), msg(2, text="ага", minutes=5), msg(3, text="да", minutes=10)]
    assert ratings_job.collect_day(CHAT, DAY, spread)[VASYA].messages == 3


def test_long_messages_are_never_merged():
    many = [msg(n, text="вот развёрнутая мысль про рендер и цены", minutes=n * 0.1)
            for n in range(1, 11)]
    assert ratings_job.collect_day(CHAT, DAY, many)[VASYA].messages == 10


def test_self_reply_gives_no_credit():
    """Прямое требование ТЗ: самореплай не приносит ответов."""
    alone = [msg(1), msg(2, reply_to=1, minutes=1)]      # отвечает сам себе
    assert ratings_job.collect_day(CHAT, DAY, alone)[VASYA].replies_got == 0


def test_reply_from_another_person_counts():
    pair = [msg(1, user=VASYA), msg(2, user=PETYA, reply_to=1, minutes=1)]
    stats = ratings_job.collect_day(CHAT, DAY, pair)

    assert stats[VASYA].replies_got == 1
    assert stats[PETYA].replies_got == 0


def test_reactions_are_capped_per_message():
    """Иначе одна накрученная реакция перевесила бы весь день."""
    loaded = [msg(1, reactions=100)]
    stats = ratings_job.collect_day(CHAT, DAY, loaded)

    assert stats[VASYA].reactions_got == ratings_job.MAX_REACTIONS_PER_MSG


# ========================================================== сбор статистики

def test_basic_counters():
    messages = [
        msg(1, text="первое сообщение про рендер https://example.com"),
        msg(2, user=PETYA, text="ответ", reply_to=1, minutes=1),
        msg(3, text="ещё одно длинное сообщение с кучей разных слов тут", minutes=30),
    ]
    stats = ratings_job.collect_day(CHAT, DAY, messages)

    assert stats[VASYA].messages == 2
    assert stats[VASYA].links == 1
    assert stats[VASYA].longest_msg >= 8
    assert stats[VASYA].replies_got == 1


def test_voice_duration_is_taken_from_raw():
    """Длительности нет в таблице — коллектор кладёт её в компактный слепок."""
    voice = msg(1, text=None, media_type="voice", raw={"duration": 65})
    stats = ratings_job.collect_day(CHAT, DAY, [voice])

    assert stats[VASYA].voice_sec == 65


def test_night_messages_use_local_timezone():
    """«Сова» — это 00:00-06:00 по таймзоне чата, а не по UTC."""
    from zoneinfo import ZoneInfo

    kam = ZoneInfo("Asia/Kamchatka")
    night = Message(
        chat_id=1, tg_msg_id=1, tg_user_id=VASYA, text="не сплю",
        date=datetime(2026, 9, 20, 3, 0, tzinfo=kam),
    )
    stats = ratings_job.collect_day(CHAT, DAY, [night], tz="Asia/Kamchatka")

    assert stats[VASYA].night_msgs == 1


def test_short_messages_are_counted_separately():
    messages = [msg(1, text="ок"), msg(2, text="развёрнутая мысль про цены", minutes=5)]
    stats = ratings_job.collect_day(CHAT, DAY, messages)

    assert stats[VASYA].messages == 2
    assert stats[VASYA].short_msgs == 1
    assert stats[VASYA].activity == pytest.approx(1.3), "короткая реплика весит 0.3"


# ============================================================ полезность

def test_usefulness_uses_thread_score_and_role():
    stats = {
        VASYA: ratings_job.AuthorDay(chat_id=1, tg_user_id=VASYA, day=DAY, messages=5),
        PETYA: ratings_job.AuthorDay(chat_id=1, tg_user_id=PETYA, day=DAY, messages=5),
    }
    contributions = [
        {"thread_id": 1, "tg_user_id": VASYA, "role": "key", "score": 0.8},
        {"thread_id": 1, "tg_user_id": PETYA, "role": "initiator", "score": 0.8},
    ]
    ratings_job.apply_usefulness(stats, contributions)

    assert stats[VASYA].usefulness > stats[PETYA].usefulness, "key весит больше initiator"


def test_roles_sum_but_are_capped():
    """Роли суммируются, но не больше 1.0 (§4.10)."""
    stats = {VASYA: ratings_job.AuthorDay(chat_id=1, tg_user_id=VASYA, day=DAY, messages=1)}
    ratings_job.apply_usefulness(stats, [
        {"thread_id": 1, "tg_user_id": VASYA, "role": "initiator", "score": 1.0},
        {"thread_id": 1, "tg_user_id": VASYA, "role": "key", "score": 1.0},
        {"thread_id": 1, "tg_user_id": VASYA, "role": "answerer", "score": 1.0},
    ])
    # 0.3 + 0.5 + 0.4 = 1.2, но потолок 1.0; плюс бонусы за реакции и ответы
    assert stats[VASYA].usefulness <= 1.0 + 0.2 + 0.3 + 0.001


def test_usefulness_scale_is_percentile():
    rows = [
        {"tg_user_id": VASYA, "usefulness": 0.1},
        {"tg_user_id": PETYA, "usefulness": 0.5},
        {"tg_user_id": MASHA, "usefulness": 0.9},
    ]
    scale = usefulness_scale(rows)

    assert scale[MASHA] == 100
    assert scale[VASYA] < scale[PETYA] < scale[MASHA]


# ============================================================== номинации

def row(user_id: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tg_user_id": user_id, "name": f"user{user_id}", "messages": 0, "short_msgs": 0,
        "words": 0, "longest_msg": 0, "voice_sec": 0, "links": 0, "replies_got": 0,
        "reactions_got": 0, "questions_answered": 0, "threads_started": 0,
        "night_msgs": 0, "usefulness": 0,
    }
    base.update(kw)
    return base


def test_active_nomination_discounts_short_replies():
    chatty = row(VASYA, messages=20, short_msgs=18)   # почти всё — «ок»
    solid = row(PETYA, messages=10, short_msgs=0)

    top = top_of(BY_KEY["active"], [chatty, solid])
    assert top[0][0]["tg_user_id"] == PETYA, "десять реплик по делу весомее двадцати «ок»"


def test_catchy_needs_enough_messages():
    """Один удачный пост не делает человека «цепляющим» (§4.10)."""
    lucky = row(VASYA, messages=2, replies_got=6)
    steady = row(PETYA, messages=20, replies_got=20)

    top = top_of(BY_KEY["catchy"], [lucky, steady])
    assert [t[0]["tg_user_id"] for t in top] == [PETYA]


def test_zero_results_are_not_shown():
    assert top_of(BY_KEY["owl"], [row(VASYA), row(PETYA)]) == []


def test_nomination_lookup_by_russian_word():
    assert find_nomination("полезный").key == "useful"
    assert find_nomination("сова").key == "owl"
    assert find_nomination("useful").key == "useful"
    assert find_nomination("ерунда") is None


def test_ranks_of_participant():
    rows = [
        row(VASYA, usefulness=0.9, links=5),
        row(PETYA, usefulness=0.5, links=10),
    ]
    places = dict((n.key, place) for n, place in ranks_of(VASYA, rows))

    assert places["useful"] == 1
    assert places["finder"] == 2


# ========================================================= разбор /top

@pytest.mark.parametrize(
    ("args", "period", "nomination"),
    [
        ("", "week", None),
        ("month", "month", None),
        ("day", "day", None),
        ("месяц полезный", "month", "useful"),
        ("полезный month", "month", "useful"),
        ("неделя сова", "week", "owl"),
    ],
)
def test_top_arguments_in_any_order(args: str, period: str, nomination: str | None):
    parsed_period, group, parsed_nomination = ratings_bot.parse_top_args(args)

    assert parsed_period == period
    assert (parsed_nomination.key if parsed_nomination else None) == nomination
    assert group is None


def test_top_arguments_with_group():
    period, group, _ = ratings_bot.parse_top_args("month #рабочая полезный")
    assert (period, group) == ("month", "рабочая")


def test_week_starts_on_monday():
    """Неделя — пн-вс (§4.10)."""
    wednesday = date(2026, 9, 23)
    period = ratings_bot.resolve_period("week", today=wednesday)

    assert period.date_from == date(2026, 9, 21), "понедельник"
    assert period.date_to == wednesday


def test_month_is_calendar():
    period = ratings_bot.resolve_period("month", today=date(2026, 9, 23))
    assert period.date_from == date(2026, 9, 1)


# ============================================================ герои дня

def test_heroes_line():
    rows = [
        row(VASYA, usefulness=0.9, questions_answered=1),
        row(PETYA, questions_answered=5, threads_started=2),
    ]
    line = ratings_bot.heroes_line(rows)

    assert line.startswith("🏅 Герои дня:")
    assert "user101" in line and "user102" in line


def test_heroes_line_is_empty_without_data():
    assert ratings_bot.heroes_line([row(VASYA), row(PETYA)]) == ""


def test_digest_shows_heroes():
    from src.digest.render import DigestData, render_html

    data = DigestData(
        chat_tg_id=-1002354231333, day=DAY, chat_title="Группа",
        highlights=["главное"], msg_count=10, participants=3,
        heroes="🏅 Герои дня: 🧠 Вася · 🛟 Петя",
    )
    text = render_html(data)

    assert "🏅 Герои дня" in text and "Вася" in text


def test_heroes_survive_the_database_roundtrip():
    from src.digest.render import DigestData

    data = DigestData(
        chat_tg_id=-100, day=DAY, heroes="🏅 Герои дня: 🧠 Вася", msg_count=1
    )
    assert DigestData.from_dict(data.to_dict()).heroes == "🏅 Герои дня: 🧠 Вася"


def test_flood_does_not_earn_words():
    """Десять «ок» подряд не должны поднимать человека в «Писателе»."""
    flood = [msg(n, text="ок", minutes=n * 0.1) for n in range(1, 11)]
    stats = ratings_job.collect_day(CHAT, DAY, flood)

    assert stats[VASYA].messages == 1
    assert stats[VASYA].words == 1, "в зачёт идёт одно сообщение, а не вся серия"


def test_links_inside_a_merged_run_still_count():
    """Ссылка не перестаёт быть ссылкой оттого, что попала в серию коротких."""
    run = [msg(n, text="ок", minutes=n * 0.1) for n in range(1, 10)]
    run.append(msg(99, text="вот https://example.com", minutes=0.95))
    stats = ratings_job.collect_day(CHAT, DAY, run)

    assert stats[VASYA].links == 1


def test_heroes_line_does_not_repeat_one_person():
    """Один человек часто берёт все три номинации — перечислять его трижды глупо."""
    rows = [row(VASYA, usefulness=0.9, questions_answered=3, threads_started=2)]
    line = ratings_bot.heroes_line(rows)

    assert line.count("user101") == 1
    assert "🧠 🛟 🔥 user101" in line
