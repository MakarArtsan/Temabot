"""Победа Саши 29.10: почему главное событие дня не попало в выпуск, и как это чинится.

Три причины, три проверки:
  - волна поздравлений от разных людей резалась на дюжину тем «без контекста» —
    модель склеивает куски одной истории, и она разбирается целиком;
  - модель не знала, что «@elheor» — это Саша, и приписала победу другому —
    в текст для неё подставляются имена;
  - выпуск собирается в 23:30, и хвост дня не попадал ни в один выпуск — окно
    выпуска теперь от 23:30 накануне до 23:30.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from src.db import repo
from src.db.models import Chat, Message
from src.digest import pipeline as dp
from src.digest import prompts
from src.digest.render import Topic
from src.llm.client import Usage
from src.nlp.threads import Thread

MSK = ZoneInfo("Europe/Moscow")
DAY = date(2026, 10, 29)
EVENING = datetime(2026, 10, 29, 22, 50, tzinfo=MSK)
SASHA, POLINA, LIZA, TANYA, DARIA = 201, 202, 203, 204, 205
NAMES = {SASHA: "Sasha Fevralskaya", POLINA: "Полина", LIZA: "Лиза", TANYA: "Татьяна",
         DARIA: "Дария"}


def msg(n: int, user: int, text: str, minutes: int, reply_to: int | None = None) -> Message:
    return Message(chat_id=1, tg_msg_id=n, tg_user_id=user, author_name=NAMES[user],
                   text=text, reply_to=reply_to, date=EVENING + timedelta(minutes=minutes))


def chat(**kw: Any) -> Chat:
    return Chat(**{"id": 1, "tg_id": -1002354231333, "title": "Группа", **kw})


# ============================================================ окно выпуска

def test_evening_issue_takes_the_tail_of_yesterday():
    start, end = repo.issue_bounds(DAY, time(23, 30), "Europe/Moscow")
    assert start == datetime(2026, 10, 28, 23, 30, tzinfo=MSK)
    assert end == datetime(2026, 10, 29, 23, 30, tzinfo=MSK)


@pytest.mark.parametrize("cut", [None, time(0, 0), time(9, 0)])
def test_morning_or_unknown_time_means_calendar_day(cut: time | None):
    assert repo.issue_bounds(DAY, cut, "Europe/Moscow") == repo.day_bounds(DAY, "Europe/Moscow")


def test_pipeline_cuts_at_the_groups_digest_time():
    assert dp.issue_cut(chat()) == time(23, 30)
    assert dp.issue_cut(chat(digest_time=time(21, 0))) == time(21, 0)


@pytest.mark.usefixtures("db")
async def test_message_after_the_digest_goes_to_the_next_issue():
    stored = await repo.bootstrap_primary_chat(-1002354231333)
    at = {
        1: datetime(2026, 10, 28, 23, 40, tzinfo=MSK),   # хвост 28-го — в выпуск 29-го
        2: datetime(2026, 10, 29, 22, 55, tzinfo=MSK),
        3: datetime(2026, 10, 29, 23, 45, tzinfo=MSK),   # после выпуска — в выпуск 30-го
    }
    for n, when in at.items():
        await repo.upsert_message(Message(chat_id=stored.id, tg_msg_id=n, tg_user_id=SASHA,
                                          author_name="Саша", text=f"сообщение {n}", date=when))

    def ids(found: list[Message]) -> list[int]:
        return [m.tg_msg_id for m in found]

    cut = time(23, 30)
    assert ids(await repo.get_messages_by_day(stored.id, DAY, tz="Europe/Moscow", cut=cut)) \
        == [1, 2]
    assert ids(await repo.get_messages_by_day(stored.id, date(2026, 10, 30),
                                              tz="Europe/Moscow", cut=cut)) == [3]
    # рейтинги и поиск по-прежнему по календарным суткам
    assert ids(await repo.get_messages_by_day(stored.id, DAY, tz="Europe/Moscow")) == [2, 3]


# ============================================================ @ник → имя

def test_handles_become_names_for_the_model():
    original = [msg(1, POLINA, "@elheor Саша???? У тебя первое место???", 0),
                msg(2, DARIA, "почта test@elheor.com и @unknown_guy", 1),
                msg(3, LIZA, "без упоминаний", 2)]

    named = dp.name_handles(original, {"elheor": "Sasha Fevralskaya"})

    assert named[0].text == "@elheor (Sasha Fevralskaya) Саша???? У тебя первое место???"
    assert named[1].text == "почта test@elheor.com и @unknown_guy", "e-mail и чужие ники — как есть"
    assert named[2] is original[2]
    assert original[0].text.startswith("@elheor Саша"), "исходные сообщения не меняются"
    assert dp.name_handles(original, {}) is original


@pytest.mark.usefixtures("db")
async def test_usernames_are_kept_only_for_people_who_wrote():
    await repo.upsert_author(SASHA, "Sasha Fevralskaya", username="@Elheor")
    await repo.upsert_author(POLINA, "Полина")
    await repo.upsert_author(POLINA, None)     # повторная запись ник и имя не стирает

    changed = await repo.save_usernames({POLINA: "polina_k", 999: "silent_member",
                                         SASHA: "Elheor"})

    assert changed == 1
    assert await repo.author_handles() == {"elheor": "Sasha Fevralskaya", "polina_k": "Полина"}
    assert await repo.find_author("999") is None, "молчащих участников в authors не заводим"


# ============================================================ склейка истории

# 29.10, вечер: Саша выиграла международный конкурс, чат узнаёт кусками
WIN = [
    msg(10, POLINA, "@elheor Саша???? У тебя первое место???", 0),
    msg(11, SASHA, "ещё идёт голосование, не итог", 1, reply_to=10),
    msg(12, LIZA, "Все, Саша выиграла??????", 3),
    msg(13, LIZA, "Поздравляем!!!!!!", 4),
    msg(14, TANYA, "@elheor что ты творишь? С победой!!!", 30),
    msg(15, TANYA, "Это ж самая крупная сумма которую мы выигрывали?", 31),
    msg(16, DARIA, "Победа Саши: первое место и $7000", 33),
    msg(17, DARIA, "смотрите ролик-победитель", 34),
]
OTHER = [
    msg(20, DARIA, "у Recraft новые правила про мошенничество", -120),
    msg(21, TANYA, "а нас за пиковые траты не забанят?", -119, reply_to=20),
    msg(22, DARIA, "если не абузить бесплатные кредиты — нет", -118, reply_to=21),
]


def win_llm(groups: Any = None):
    calls: list[dict[str, Any]] = []

    async def call(messages: list[dict[str, str]], *, purpose: str, **kw: Any) -> Any:
        user = messages[-1]["content"]
        calls.append({"purpose": purpose, "system": messages[0]["content"], "user": user})
        if purpose == "stories":
            if isinstance(groups, Exception):
                raise groups
            return {"groups": groups or []}, Usage(30, 10)
        if purpose == "score":
            return {"kind": "announcement", "usefulness": 9, "specificity": 8,
                    "relevance": 9, "takeaway": "вывод", "why": "важно"}, Usage(10, 5)
        if purpose != "summary":
            return {}, Usage(10, 5)
        if "Recraft" in user:
            return {"title": "Recraft ужесточил правила"}, Usage(20, 10)
        if "$7000" in user and "первое место" in user:
            return {"title": "Саша Февральская взяла первое место и $7000",
                    "summary": "Саша выиграла международный конкурс хорроров"}, Usage(40, 20)
        return {"title": "Кто-то что-то спрашивает без контекста"}, Usage(20, 10)

    call.calls = calls  # type: ignore[attr-defined]
    return call


def fragments() -> list[tuple[Thread, Topic, Any]]:
    """Как резала программа: каждый со своим заголовком «без контекста»."""
    parts = [WIN[0:2], WIN[2:4], WIN[4:6], WIN[6:8], OTHER]
    result = []
    for n, part in enumerate(parts):
        thread = Thread(chat_id=1, root_msg_id=part[0].tg_msg_id, messages=list(part))
        topic = Topic(thread_id=thread.root_msg_id, title=f"кусок {n}",
                      summary=" ".join(m.text or "" for m in part),
                      participants=sorted({m.author_name or "" for m in part}))
        result.append((thread, topic, f"сигналы {n}"))
    return result


def signals_of(thread: Thread) -> str:
    return f"сигналы склейки {len(thread.messages)}"


async def test_pieces_of_one_story_become_one_topic():
    llm = win_llm(groups=[[2, 3, 4, 5]])     # по времени: Recraft (1), потом победа

    stories, usage = await dp.merge_stories(fragments(), chat(), signals_of, llm=llm)

    titles = [topic.title for _, topic, _ in stories]
    assert titles == ["Саша Февральская взяла первое место и $7000", "кусок 4"]
    thread, topic, signals = stories[0]
    assert [m.tg_msg_id for m in thread.messages] == [10, 11, 12, 13, 14, 15, 16, 17]
    assert thread.root_msg_id == 10 and topic.thread_id == 10
    assert signals == "сигналы склейки 8", "сигналы — по всей истории: все участники"
    assert usage.tokens_in == 30 + 40
    stories_call = llm.calls[0]
    assert stories_call["purpose"] == "stories" and stories_call["system"] == prompts.STORIES_SYSTEM
    assert "1. [20:50–20:52] кусок 4" in stories_call["user"], "время по Москве, по порядку"
    # склейку разбирают целиком и без деления обратно на куски
    summary_call = llm.calls[1]
    assert summary_call["purpose"] == "summary"
    assert prompts.MAP_SPLIT_HINT not in summary_call["system"]


@pytest.mark.parametrize("answer", [
    [], [[1]], [[2, 99]], [["два", 3]], "мусор", RuntimeError("модель легла"),
])
async def test_nothing_to_merge_leaves_topics_as_they_were(answer: Any):
    before = fragments()
    stories, _ = await dp.merge_stories(before, chat(), signals_of, llm=win_llm(groups=answer))
    assert [t.title for _, t, _ in stories] == [t.title for _, t, _ in before]


def test_story_groups_are_checked():
    assert dp.story_groups({"groups": [[3, 1], [1, 2], [4, 4, 5], [0, 6], "x"]}, 5) == [
        [1, 3], [4, 5]], "номер — только в одной группе, только настоящий"
    assert dp.story_groups([], 5) == []


async def test_too_few_topics_cost_nothing():
    llm = win_llm(groups=[[1, 2]])
    few = fragments()[:3]
    stories, usage = await dp.merge_stories(few, chat(), signals_of, llm=llm)
    assert stories == few and llm.calls == [] and usage.tokens_in == 0


async def test_huge_merge_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dp, "STORY_MAX_MESSAGES", 5)
    stories, _ = await dp.merge_stories(fragments(), chat(), signals_of,
                                        llm=win_llm(groups=[[2, 3, 4, 5]]))
    assert len(stories) == 5, "полдня чата одной «историей» не склеиваем"


def scrap(n: int, user: int, text: str, minutes: int) -> Thread:
    return Thread(chat_id=1, root_msg_id=n, messages=[msg(n, user, text, minutes)])


async def test_short_congratulations_join_the_story():
    """«Поздравляю!» по отдельности не разбирается, но к истории присоединяется."""
    scraps = [scrap(30, DARIA, "Поздравляю!!!", 35), scrap(31, POLINA, "ура", 36)]
    llm = win_llm(groups=[[2, 3, 4, 5, 6, 7], [6, 7]])   # 6 и 7 — реплики

    stories, _ = await dp.merge_stories(fragments(), chat(), signals_of, llm=llm,
                                        scraps=scraps)

    thread, topic, _ = stories[0]
    assert topic.title.startswith("Саша Февральская")
    assert [m.tg_msg_id for m in thread.messages][-2:] == [30, 31]
    assert "6. [23:25] реплика — Дария: Поздравляю!!!" in llm.calls[0]["user"]


async def test_scraps_alone_are_not_a_story():
    scraps = [scrap(30, DARIA, "Поздравляю!!!", 35), scrap(31, POLINA, "ура", 36)]
    stories, _ = await dp.merge_stories(fragments(), chat(), signals_of,
                                        llm=win_llm(groups=[[6, 7]]), scraps=scraps)
    assert [t.title for _, t, _ in stories] == [t.title for _, t, _ in fragments()]


async def test_scraps_are_left_out_when_the_list_would_be_too_long(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(dp, "STORY_MAX_LINES", 6)
    llm = win_llm()
    scraps = [scrap(30 + i, DARIA, "ура", 35 + i) for i in range(2)]
    await dp.merge_stories(fragments(), chat(), signals_of, llm=llm, scraps=scraps)
    assert "реплика" not in llm.calls[0]["user"]


async def test_the_win_reaches_the_digest(monkeypatch: pytest.MonkeyPatch):
    """Сборка дня целиком: победа — одна тема, по всем сообщениям и всем людям."""
    seen: dict[str, Any] = {}

    async def get_messages_by_day(chat_id: int, day: Any, **kw: Any) -> list[Message]:
        seen.update(kw)
        return OTHER + WIN

    async def nothing(*a: Any, **kw: Any) -> Any:
        return []

    async def no_history(*a: Any, **kw: Any) -> dict[str, Any]:
        return {}

    async def weights() -> dict[str, Any]:
        return {"weights": {}, "muted": set()}

    async def handles() -> dict[str, str]:
        return {"elheor": "Sasha Fevralskaya"}

    async def no_lore(chat: Any) -> str:
        return ""

    async def no_embedding(text: str) -> list[float]:
        raise RuntimeError("эмбеддинги в тесте не нужны")

    from src.scoring import novelty as novelty_mod

    for name, fn in {"get_messages_by_day": get_messages_by_day,
                     "list_author_weights": weights, "signal_history": no_history,
                     "recent_topics": nothing, "feedback_examples": nothing,
                     "author_handles": handles}.items():
        monkeypatch.setattr(dp.repo, name, fn)
    monkeypatch.setattr(dp, "lore_context", no_lore)
    monkeypatch.setattr(novelty_mod, "embed_one", no_embedding)

    def groups_of_win(user: str) -> list[list[int]]:
        lines = [line for line in user.splitlines() if line[:1].isdigit()]
        return [[i for i, line in enumerate(lines, start=1) if "Recraft" not in line]]

    llm = win_llm()

    async def call(messages: list[dict[str, str]], *, purpose: str, **kw: Any) -> Any:
        if purpose == "stories":
            return {"groups": groups_of_win(messages[-1]["content"])}, Usage(30, 10)
        return await llm(messages, purpose=purpose, **kw)

    result = await dp.build_digest(chat(), DAY, llm=call)

    assert seen.get("cut") == time(23, 30)
    win = next(t for t in result.all_topics if "первое место" in t.title)
    assert win.msg_count == len(WIN)
    assert sum("первое место" in t.title for t in result.all_topics) == 1
    summaries = [c["user"] for c in llm.calls if c["purpose"] == "summary"]
    assert any("@elheor (Sasha Fevralskaya)" in text for text in summaries), \
        "модель знает, кого поздравляют"


async def test_merge_needs_a_real_one_day_order(monkeypatch: pytest.MonkeyPatch):
    """Время в списке для модели — по часовому поясу группы, а не UTC."""
    part = Thread(chat_id=1, root_msg_id=1, messages=[
        Message(chat_id=1, tg_msg_id=1, tg_user_id=SASHA, author_name="Саша", text="x",
                date=datetime(2026, 10, 29, 19, 51, tzinfo=UTC)),
    ])
    line = dp._story_line(7, part, Topic(thread_id=1, title="Заголовок", summary="Суть",
                                        participants=["Саша"]))
    assert line == "7. [22:51–22:51] Заголовок — Суть (участники: Саша)"
