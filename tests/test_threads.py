"""Тесты сегментации на треды (TZ §4.2)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.db.models import Message
from src.nlp.threads import segment

START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

VASYA, PETYA, MASHA = 101, 102, 103


def msg(
    tg_msg_id: int,
    *,
    user: int = VASYA,
    text: str = "сообщение",
    minutes: int = 0,
    reply_to: int | None = None,
    topic_id: int | None = None,
) -> Message:
    return Message(
        chat_id=1,
        tg_msg_id=tg_msg_id,
        tg_user_id=user,
        author_name=f"user{user}",
        text=text,
        reply_to=reply_to,
        topic_id=topic_id,
        date=START + timedelta(minutes=minutes),
    )


def ids(threads) -> list[list[int]]:
    return [t.msg_ids for t in threads]


# ------------------------------------------------------------ базовые случаи

def test_empty_input():
    assert segment([]) == []


def test_reply_chain_becomes_one_thread():
    threads = segment([
        msg(1, user=VASYA, text="как ускорить рендер?"),
        msg(2, user=PETYA, reply_to=1, minutes=2),
        msg(3, user=MASHA, reply_to=2, minutes=5),
    ])
    assert ids(threads) == [[1, 2, 3]], "цепочка ответов склеивается транзитивно"


def test_reply_to_old_message_groups_siblings():
    """Ответы на одно вчерашнее сообщение должны оказаться вместе."""
    threads = segment([
        msg(10, user=PETYA, reply_to=999),
        msg(11, user=MASHA, reply_to=999, minutes=40),
    ])
    assert ids(threads) == [[10, 11]]
    assert threads[0].root_msg_id == 10, "корня нет в выборке — тред зовётся по первому"


def test_two_independent_conversations():
    threads = segment([
        msg(1, user=VASYA, text="про рендер"),
        msg(2, user=PETYA, reply_to=1, minutes=1),
        msg(50, user=MASHA, text="совсем другая тема", minutes=200),
        msg(51, user=MASHA, reply_to=50, minutes=201),
    ])
    assert ids(threads) == [[1, 2], [50, 51]]


# ------------------------------------------------- приклеивание по времени

def test_message_sticks_to_recent_thread_of_same_author():
    """Человек дописывает мысль без реплая — это тот же разговор."""
    threads = segment([
        msg(1, user=VASYA, text="думаю про Seedance"),
        msg(2, user=PETYA, reply_to=1, minutes=2),
        msg(3, user=VASYA, text="и ещё вот что", minutes=5),
    ])
    assert ids(threads) == [[1, 2, 3]]


def test_long_pause_starts_new_thread():
    """Больше 25 минут тишины — это уже другой разговор."""
    threads = segment([
        msg(1, user=VASYA),
        msg(2, user=PETYA, reply_to=1, minutes=1),
        msg(3, user=VASYA, minutes=40),
    ])
    assert ids(threads) == [[1, 2], [3]]


def test_new_person_starts_new_thread():
    """Участники не пересекаются — приклеивать нельзя, даже если прошло две минуты."""
    threads = segment([
        msg(1, user=VASYA),
        msg(2, user=VASYA, reply_to=1, minutes=1),
        msg(3, user=MASHA, text="а у меня свой вопрос?", minutes=2),
    ])
    assert ids(threads) == [[1, 2], [3]]


def test_gap_is_measured_from_last_message_not_from_start():
    """Разговор длиной в час не рвётся, пока в нём не молчат 25 минут."""
    threads = segment([
        msg(1, user=VASYA),
        msg(2, user=PETYA, reply_to=1, minutes=20),
        msg(3, user=VASYA, minutes=40),
        msg(4, user=VASYA, minutes=55),
    ])
    assert ids(threads) == [[1, 2, 3, 4]]


def test_reply_wins_over_time_window():
    """Ответ на старое сообщение продолжает тот тред, а не последний активный."""
    threads = segment([
        msg(1, user=VASYA, text="первая тема"),
        msg(2, user=PETYA, reply_to=1, minutes=1),
        msg(3, user=MASHA, text="вторая тема?", minutes=60),
        msg(4, user=VASYA, reply_to=1, minutes=61),
    ])
    assert ids(threads) == [[1, 2, 4], [3]]


# ------------------------------------------------------------ форумные темы

def test_threads_never_cross_forum_topics():
    threads = segment([
        msg(1, user=VASYA, topic_id=100),
        msg(2, user=VASYA, topic_id=200, minutes=1),
        msg(3, user=VASYA, topic_id=100, minutes=2),
    ])
    assert ids(threads) == [[1, 3], [2]], "тема — жёсткая граница"
    assert threads[0].topic_id == 100
    assert threads[1].topic_id == 200


def test_time_window_applies_inside_a_topic():
    """Тема живёт неделями, а тред внутри неё — нет."""
    threads = segment([
        msg(1, user=VASYA, topic_id=100),
        msg(2, user=VASYA, topic_id=100, minutes=5),
        msg(3, user=VASYA, topic_id=100, minutes=300),
    ])
    assert ids(threads) == [[1, 2], [3]]


# ---------------------------------------------------------------- low_value

def test_short_thread_without_question_is_low_value():
    threads = segment([msg(1, user=VASYA, text="+"), msg(2, user=PETYA, text="ок", minutes=1)])
    assert threads[0].low_value is True


def test_short_thread_with_question_is_not_low_value():
    """Вопрос важен, даже если на него никто не ответил (§4.3, «Без ответа»)."""
    threads = segment([msg(1, user=VASYA, text="кто-нибудь пробовал Seedance?")])
    assert threads[0].low_value is False


def test_three_messages_are_enough():
    threads = segment([
        msg(1, user=VASYA, text="раз"),
        msg(2, user=PETYA, reply_to=1, text="два", minutes=1),
        msg(3, user=MASHA, reply_to=1, text="три", minutes=2),
    ])
    assert threads[0].low_value is False


def test_question_in_voice_transcript_counts():
    """Голосовое с вопросом — такой же вопрос."""
    voice = Message(
        chat_id=1, tg_msg_id=1, tg_user_id=VASYA, media_type="voice",
        transcript="а кто знает как это работает?", date=START,
    )
    assert segment([voice])[0].low_value is False


# ------------------------------------------------------------- свойства треда

def test_thread_properties():
    threads = segment([
        msg(1, user=VASYA, text="вопрос?"),
        msg(2, user=PETYA, reply_to=1, minutes=10),
        msg(3, user=MASHA, reply_to=1, minutes=30),
    ])
    thread = threads[0]

    assert thread.root_msg_id == 1, "тред зовётся по своему первому сообщению"
    assert thread.participants == {VASYA, PETYA, MASHA}
    assert thread.duration_min == 30
    assert thread.has_question is True
    assert thread.chat_id == 1


def test_thread_text_is_readable_for_the_model():
    threads = segment([
        msg(1, user=VASYA, text="привет"),
        msg(2, user=PETYA, reply_to=1, text="здорово", minutes=1),
    ])
    text = threads[0].text
    assert "[1] user101: привет" in text
    assert "[2] user102: здорово" in text


def test_media_without_text_is_marked():
    voice = Message(
        chat_id=1, tg_msg_id=7, tg_user_id=VASYA, media_type="voice", date=START
    )
    assert "[voice]" in segment([voice])[0].text


def test_messages_out_of_order_are_sorted():
    threads = segment([
        msg(3, user=VASYA, minutes=2),
        msg(1, user=VASYA),
        msg(2, user=VASYA, minutes=1),
    ])
    assert ids(threads) == [[1, 2, 3]]


def test_threads_are_ordered_by_start_time():
    threads = segment([
        msg(50, user=MASHA, text="поздняя тема?", minutes=100),
        msg(1, user=VASYA, text="ранняя тема?"),
    ])
    assert [t.root_msg_id for t in threads] == [1, 50]
