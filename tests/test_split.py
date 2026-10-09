"""Склеенные разговоры: длинное обсуждение модель делит, и каждый пересказывается сам.

Программа режет переписку по ответам и времени. В плотном чате, где разговоры
идут вперемешку, в одно обсуждение попадают куски чужих — и пересказ сводил
разные истории в одну (жалоба владельца, 09.10).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from src.db.models import Chat, Message
from src.digest import pipeline as dp
from src.digest import prompts
from src.llm.client import Usage
from src.nlp.threads import Thread

START = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
VASYA, PETYA, MASHA, OLYA = 101, 102, 103, 104
GOOD_RUBRIC = {"kind": "insight", "usefulness": 8, "specificity": 7, "relevance": 8,
               "takeaway": "вывод", "why": "важно"}


def msg(n: int, user: int, text: str, minutes: int, reply_to: int | None = None) -> Message:
    return Message(chat_id=1, tg_msg_id=n, tg_user_id=user, author_name=f"user{user}",
                   text=text, reply_to=reply_to, date=START + timedelta(minutes=minutes))


# хакатон (1, 3, 5, 7) и хоррор-конкурс (2, 4, 6, 8) вперемешку, плюс реплика в сторону (9)
GLUED = [
    msg(1, VASYA, "на хакатоне нет призового фонда", 0),
    msg(2, MASHA, "сдала ролик на хоррор-конкурс", 1),
    msg(3, PETYA, "зачем тогда ехать на хакатон?", 2, reply_to=1),
    msg(4, OLYA, "поздравляю с хоррором!", 3, reply_to=2),
    msg(5, VASYA, "ради нетворкинга и брендов", 4, reply_to=3),
    msg(6, MASHA, "загрузка на сайт конкурса зависала", 5),
    msg(7, PETYA, "ок, едем на хакатон", 6),
    msg(8, OLYA, "монстров в ролике нет, но жутко", 7),
    msg(9, PETYA, "у кого есть промокод в Runway?", 8),
]


def thread(messages: list[Message]) -> Thread:
    return Thread(chat_id=1, root_msg_id=messages[0].tg_msg_id, messages=list(messages))


def chat() -> Chat:
    return Chat(id=1, tg_id=-1002354231333, title="Группа")


def fake_llm(split: Any = None):
    """Модель: на склеенный тред с подсказкой — split, на части — пересказ по содержанию."""
    calls: list[dict[str, Any]] = []

    async def call(messages: list[dict[str, str]], *, purpose: str, **kw: Any) -> Any:
        system, user = messages[0]["content"], messages[-1]["content"]
        calls.append({"purpose": purpose, "split_offered": prompts.MAP_SPLIT_HINT in system,
                      "text": user})
        if purpose == "score":
            return GOOD_RUBRIC, Usage(10, 5)
        if purpose != "summary":
            return {}, Usage(10, 5)
        if prompts.MAP_SPLIT_HINT in system and split is not None:
            return {"split": split, "title": "склейка, не должна попасть в дайджест"}, Usage(50, 20)
        if "хакатон" in user and "хоррор" in user:
            return {"title": "Всё в кучу: хакатон и хоррор"}, Usage(50, 20)
        if "хакатон" in user:
            return {"title": "Хакатон без призового фонда", "summary": "Едут ради нетворкинга"}, \
                Usage(40, 20)
        if "хоррор" in user or "конкурс" in user:
            return {"title": "Маша сдала ролик на хоррор-конкурс",
                    "open_questions": ["кто ещё участвует?"]}, Usage(40, 20)
        return {"title": "Что-то ещё"}, Usage(40, 20)

    call.calls = calls  # type: ignore[attr-defined]
    return call


async def test_glued_thread_becomes_two_separate_topics():
    llm = fake_llm(split=[[1, 3, 5, 7], [2, 4, 6, 8]])
    result, usage = await dp.map_conversations(thread(GLUED), chat(), llm=llm)

    titles = [topic.title for _, topic in result if topic]
    assert titles == ["Хакатон без призового фонда", "Маша сдала ролик на хоррор-конкурс"]
    parts = [part for part, _ in result]
    assert [p.msg_ids for p in parts] == [[1, 3, 5, 7], [2, 4, 6, 8]]
    assert [topic.thread_id for _, topic in result if topic] == [1, 2]
    # в пересказ хакатона не попала ни одна реплика про хоррор и наоборот
    hackathon_text = llm.calls[1]["text"]
    assert "хоррор" not in hackathon_text and "промокод" not in hackathon_text
    assert not any(c["split_offered"] for c in llm.calls[1:]), "части повторно не делим"
    assert usage.tokens_in == 50 + 40 + 40


async def test_one_conversation_is_left_whole():
    llm = fake_llm(split=None)
    hackathon = [m for m in GLUED if m.tg_msg_id in (1, 3, 5, 7)] + [
        msg(10, MASHA, "а что с хакатоном по датам?", 9),
        msg(11, VASYA, "хакатон перенесли на 20-е", 10, reply_to=10),
    ]
    result, _ = await dp.map_conversations(thread(hackathon), chat(), llm=llm)
    assert len(result) == 1 and result[0][1] is not None
    assert result[0][1].title == "Хакатон без призового фонда"
    assert llm.calls[0]["split_offered"] is True


async def test_short_thread_is_never_offered_a_split():
    llm = fake_llm(split=[[1, 3], [2, 4]])
    short = thread(GLUED[:4])
    result, _ = await dp.map_conversations(short, chat(), llm=llm)
    assert len(result) == 1
    assert llm.calls[0]["split_offered"] is False


async def test_side_remarks_are_dropped_not_made_into_topics():
    """Часть из 1–2 реплик — это реплика в сторону, отдельной темой её не делаем."""
    llm = fake_llm(split=[[1, 3, 5, 7], [2, 4, 6, 8], [9]])
    result, _ = await dp.map_conversations(thread(GLUED), chat(), llm=llm)
    assert [p.msg_ids for p, _ in result] == [[1, 3, 5, 7], [2, 4, 6, 8]]


async def test_only_one_real_conversation_after_cleanup():
    """Модель отделила только реплику в сторону — пересказываем основной разговор без неё."""
    llm = fake_llm(split=[[1, 2, 3, 4, 5, 6, 7, 8], [9]])
    result, _ = await dp.map_conversations(thread(GLUED), chat(), llm=llm)
    assert [p.msg_ids for p, _ in result] == [[1, 2, 3, 4, 5, 6, 7, 8]]
    assert "промокод" not in llm.calls[1]["text"]


@pytest.mark.parametrize("bad", [
    [[1, 3], [2, 4]],             # все части слишком короткие
    [[999, 1000, 1001]],          # номера не из этого треда
    [["раз", "два", "три"]],      # не номера вовсе
])
async def test_bad_split_falls_back_to_the_whole_thread(bad: Any):
    llm = fake_llm(split=bad)
    result, _ = await dp.map_conversations(thread(GLUED), chat(), llm=llm)
    assert len(result) == 1 and result[0][0].msg_ids == [m.tg_msg_id for m in GLUED]
    assert result[0][1] is not None and "склейка" not in result[0][1].title
    assert llm.calls[1]["split_offered"] is False


async def test_message_is_never_in_two_conversations():
    llm = fake_llm(split=[[1, 3, 5, 7], [1, 2, 4, 6, 8]])
    result, _ = await dp.map_conversations(thread(GLUED), chat(), llm=llm)
    ids = [i for part, _ in result for i in part.msg_ids]
    assert len(ids) == len(set(ids))


async def test_split_reaches_the_digest(monkeypatch: pytest.MonkeyPatch):
    """Сборка дня: две отдельные темы, у каждой свой «без ответа» и свои участники."""
    # как в живой теме: те же люди, без «ответов», два разговора одновременно —
    # программа склеивает всё в один тред
    interleaved = [
        msg(1, VASYA, "на хакатоне нет призового фонда", 0),
        msg(2, PETYA, "зачем тогда ехать на хакатон?", 1, reply_to=1),
        msg(3, VASYA, "хакатон ради нетворкинга", 2, reply_to=2),
        msg(4, PETYA, "кстати, сдал ролик на хоррор-конкурс", 3),
        msg(5, VASYA, "загрузка на сайт конкурса не зависала?", 4),
        msg(6, PETYA, "зависала, в хорроре монстров нет, но жутко", 5),
        msg(7, VASYA, "итоги конкурса когда?", 6),
        msg(8, PETYA, "завтра огласят итоги конкурса", 7),
    ]
    assert len(dp.segment(interleaved)) == 1, "сценарий: программа склеила всё в один тред"

    async def get_messages_by_day(chat_id: int, day: Any, **kw: Any) -> list[Message]:
        return list(interleaved)

    async def nothing(*a: Any, **kw: Any) -> Any:
        return []

    async def no_history(*a: Any, **kw: Any) -> dict[str, Any]:
        return {}

    async def weights() -> dict[str, Any]:
        return {"weights": {}, "muted": set()}

    async def no_lore(chat: Any) -> str:
        return ""

    monkeypatch.setattr(dp.repo, "get_messages_by_day", get_messages_by_day)
    monkeypatch.setattr(dp.repo, "list_author_weights", weights)
    monkeypatch.setattr(dp.repo, "signal_history", no_history)
    monkeypatch.setattr(dp.repo, "recent_topics", nothing)
    monkeypatch.setattr(dp.repo, "feedback_examples", nothing)
    monkeypatch.setattr(dp, "lore_context", no_lore)

    async def no_embedding(text: str) -> list[float]:
        raise RuntimeError("эмбеддинги в тесте не нужны")

    from src.scoring import novelty as novelty_mod

    monkeypatch.setattr(novelty_mod, "embed_one", no_embedding)

    # всё одним сообщением подряд — без ответов программа склеила бы это в один тред
    result = await dp.build_digest(
        chat(), date(2026, 10, 8), llm=fake_llm(split=[[1, 2, 3], [4, 5, 6, 7, 8]])
    )
    titles = {t.title for t in result.all_topics}
    assert "Хакатон без призового фонда" in titles
    assert "Маша сдала ролик на хоррор-конкурс" in titles
    assert not any("кучу" in t or "склейка" in t for t in titles)
    assert len(result.all_topics) == 2
    horror = next(t for t in result.all_topics if "хоррор" in t.title)
    assert horror.thread_id == 4 and horror.msg_count == 5
    if horror in result.topics:
        assert ("кто ещё участвует?", horror.anchor_msg_id) in result.data.unanswered
