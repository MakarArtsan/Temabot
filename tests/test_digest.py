"""Тесты дайджеста и LLM-адаптера (TZ шаг 6). Настоящая модель не вызывается."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from src.db.models import Chat, Message
from src.digest import pipeline as dp
from src.digest.render import DigestData, Topic, deeplink, render
from src.llm.client import Usage, extract_json
from src.nlp.threads import segment

START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
CHAT_TG_ID = -1002354231333
VASYA, PETYA, MASHA = 101, 102, 103


def chat(**kw: Any) -> Chat:
    defaults: dict[str, Any] = dict(id=1, tg_id=CHAT_TG_ID, title="Тестовая группа")
    defaults.update(kw)
    return Chat(**defaults)


def msg(
    tg_msg_id: int, *, user: int = VASYA, text: str = "текст", minutes: int = 0,
    reply_to: int | None = None, media_type: str | None = None, reactions: int = 0,
) -> Message:
    return Message(
        chat_id=1, tg_msg_id=tg_msg_id, tg_user_id=user, author_name=f"user{user}",
        text=text, reply_to=reply_to, media_type=media_type, reactions=reactions,
        date=START + timedelta(minutes=minutes),
    )


# ======================================================== LLM: разбор ответа

def test_extract_json_plain():
    assert extract_json('{"title": "тема"}') == {"title": "тема"}


def test_extract_json_in_markdown_fence():
    """Модели заворачивают ответ в ```json, даже когда просишь не надо."""
    assert extract_json('```json\n{"title": "тема"}\n```') == {"title": "тема"}
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_chatter_around():
    text = 'Вот результат:\n{"title": "тема"}\nНадеюсь, помог!'
    assert extract_json(text) == {"title": "тема"}


def test_extract_json_array():
    assert extract_json("[1, 2, 3]") == [1, 2, 3]


@pytest.mark.parametrize("bad", ["", "   ", "совсем не json", "{сломано"])
def test_extract_json_raises_on_garbage(bad: str):
    with pytest.raises(ValueError):
        extract_json(bad)


def test_usage_adds_up():
    total = Usage(10, 5, "m") + Usage(3, 2, "m")
    assert (total.tokens_in, total.tokens_out) == (13, 7)


# ============================================================ фильтр шума

@pytest.mark.parametrize(
    "text", ["+", "ок", "Спасибо!", "ага", "плюсую", "круто", "понял", "++"]
)
def test_short_reactions_are_noise(text: str):
    assert dp.is_noise(msg(1, text=text)) is True


@pytest.mark.parametrize(
    "text",
    [
        "спасибо, помогло — надо было включить кэш",
        "нет, так не работает",
        "а почему?",
        "цена 20 долларов",
    ],
)
def test_meaningful_short_messages_are_not_noise(text: str):
    assert dp.is_noise(msg(1, text=text)) is False


def test_stickers_are_noise():
    assert dp.is_noise(msg(1, text="", media_type="sticker")) is True


def test_voice_without_transcript_is_not_noise():
    """Голосовое ещё не расшифровано — выбрасывать его нельзя."""
    assert dp.is_noise(msg(1, text="", media_type="voice")) is False


def test_split_noise_counts_both_sides():
    meaningful, noise = dp.split_noise([
        msg(1, text="как ускорить рендер?"),
        msg(2, text="+"),
        msg(3, text="надо включить кэш, тогда быстрее"),
        msg(4, text="спасибо"),
    ])
    assert [m.tg_msg_id for m in meaningful] == [1, 3]
    assert [m.tg_msg_id for m in noise] == [2, 4]


# ============================================================== map-стадия

GOOD_RUBRIC = {
    "kind": "decision",
    "usefulness": 8,
    "specificity": 7,
    "relevance": 8,
    "takeaway": "вывод одной строкой",
    "why": "три человека независимо поймали",
}


def fake_llm(payload: Any, *, fail: bool = False, rubric: Any = None):
    """Подделка chat_json.

    Конвейер делает два вызова на тред: разбор (purpose=summary) и оценку по
    рубрике (purpose=score). По умолчанию рубрика отвечает «хорошей» темой,
    иначе всё отсеивалось бы порогом.
    """
    calls: list[dict[str, Any]] = []

    async def call(messages, *, purpose: str, chat_id: int | None = None, **kw: Any):
        calls.append({"messages": messages, "purpose": purpose, "chat_id": chat_id})
        if fail:
            raise RuntimeError("модель недоступна")
        if purpose == "score":
            return (rubric if rubric is not None else GOOD_RUBRIC), Usage(80, 40, "test-model")
        value = payload(calls) if callable(payload) else payload
        return value, Usage(100, 50, "test-model")

    call.calls = calls  # type: ignore[attr-defined]
    return call


def a_thread() -> Any:
    return segment([
        msg(1, user=VASYA, text="Seedance режет 15-секундные ролики?"),
        msg(2, user=PETYA, reply_to=1, text="да, референсы больше 2K обрезаются", minutes=2),
        msg(3, user=MASHA, reply_to=1, text="ужимай заранее", minutes=5, reactions=3),
    ])[0]


async def test_map_thread_builds_topic():
    llm = fake_llm({
        "title": "Seedance режет 15-сек ролики",
        "decision": "референсы больше 2K ужимать заранее",
        "debate": "",
        "open_questions": ["а что с 4K?"],
        "links": ["https://example.com/docs"],
        "mentions": ["Seedance", "2K"],
        "key_msg_ids": [2],
        "contributors": [{"tg_user_id": VASYA, "role": "initiator"}],
    })

    topic, usage = await dp.map_thread(a_thread(), chat(), llm=llm)

    assert topic is not None
    assert topic.title == "Seedance режет 15-сек ролики"
    assert topic.decision == "референсы больше 2K ужимать заранее"
    assert topic.key_msg_ids == [2]
    assert topic.msg_count == 3
    assert topic.reactions == 3
    assert usage.tokens_in == 100
    assert llm.calls[0]["purpose"] == "summary"


async def test_map_thread_drops_invented_message_ids():
    """Модель любит выдумывать номера — в ссылку такое пускать нельзя."""
    llm = fake_llm({"title": "тема", "key_msg_ids": [2, 999, "abc"]})
    topic, _ = await dp.map_thread(a_thread(), chat(), llm=llm)

    assert topic is not None and topic.key_msg_ids == [2]


async def test_map_thread_drops_outsiders_from_contributors():
    """В рейтинги (§4.10) не должны попадать те, кто в треде не писал."""
    llm = fake_llm({
        "title": "тема",
        "contributors": [
            {"tg_user_id": VASYA, "role": "key"},
            {"tg_user_id": 999, "role": "key"},          # не участвовал
            {"tg_user_id": PETYA, "role": "гений"},      # роли такой нет
        ],
    })
    topic, _ = await dp.map_thread(a_thread(), chat(), llm=llm)

    assert topic is not None
    assert topic.contributors == [{"tg_user_id": VASYA, "role": "key"}]


async def test_empty_title_means_thread_is_skipped():
    topic, _ = await dp.map_thread(a_thread(), chat(), llm=fake_llm({"title": "  "}))
    assert topic is None


async def test_map_thread_propagates_llm_failure():
    """Сбой модели должен быть виден снаружи: тишину легко принять за пустой день."""
    with pytest.raises(RuntimeError, match="модель недоступна"):
        await dp.map_thread(a_thread(), chat(), llm=fake_llm({}, fail=True))


async def test_interests_profile_goes_into_the_prompt():
    llm = fake_llm({"title": "тема"})
    await dp.map_thread(
        a_thread(), chat(settings={"interests_profile": "интересно: AI-видео"}), llm=llm
    )
    system = llm.calls[0]["messages"][0]["content"]
    assert "интересно: AI-видео" in system


# =============================================================== reduce

def topic(title: str, **kw: Any) -> Topic:
    defaults: dict[str, Any] = dict(thread_id=1, title=title)
    defaults.update(kw)
    return Topic(**defaults)


async def test_highlights_are_limited_to_three():
    llm = fake_llm({"highlights": ["раз", "два", "три", "четыре"]})
    highlights, _ = await dp.make_highlights([topic("тема")], chat(), llm=llm)
    assert highlights == ["раз", "два", "три"]


async def test_highlights_without_topics_skip_the_call():
    llm = fake_llm({"highlights": ["не должно вызваться"]})
    highlights, _ = await dp.make_highlights([], chat(), llm=llm)

    assert highlights == []
    assert llm.calls == [], "лишний вызов модели — это лишние деньги"


# =============================================================== рендер

def test_deeplink_strips_supergroup_prefix():
    assert deeplink(-1002354231333, 1234) == "https://t.me/c/2354231333/1234"


def test_render_full_digest():
    data = DigestData(
        chat_tg_id=CHAT_TG_ID,
        day=date(2026, 9, 20),
        chat_title="Тестовая группа",
        highlights=["Seedance обрезает хвост у 4K-референсов"],
        topics=[
            topic(
                "Seedance режет 15-сек ролики",
                thread_id=1,
                decision="референсы больше 2K ужимать заранее",
                mentions=["Seedance"],
                key_msg_ids=[2],
                participants=["Вася", "Петя"],
                msg_count=3,
                reactions=3,
            )
        ],
        unanswered=[("а что с 4K?", 5)],
        links=["https://example.com"],
        msg_count=42,
        participants=5,
        noise_count=7,
    )
    text = render(data)

    assert "📌 *Главное за день*" in text
    assert "Вывод: референсы больше 2K ужимать заранее" in text
    assert "https://t.me/c/2354231333/2" in text, "ссылка ведёт на ключевое сообщение"
    assert "❓ *Без ответа*" in text
    assert "🔗 *Ссылки дня*" in text
    assert "42 сообщений" in text and "5 участников" in text
    assert "7 коротких реплик не в счёт" in text


def test_render_empty_day():
    text = render(DigestData(chat_tg_id=CHAT_TG_ID, day=date(2026, 9, 20), msg_count=3))
    assert "ничего заметного не обсуждали" in text


def test_link_falls_back_to_thread_start():
    """Если модель не назвала ключевое сообщение, ведём в начало треда."""
    assert topic("тема", thread_id=77).anchor_msg_id == 77
    assert topic("тема", thread_id=77, key_msg_ids=[80]).anchor_msg_id == 80


# ========================================================= сборка целиком

@pytest.fixture(autouse=True)
def no_scoring_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Скоринг читает веса авторов, историю сигналов и примеры оценок."""
    async def author_weights() -> dict[str, Any]:
        return {"weights": {}, "muted": set()}

    async def empty_history(*a: Any, **kw: Any) -> dict[str, list[float]]:
        return {}

    async def empty_list(*a: Any, **kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(dp.repo, "list_author_weights", author_weights)
    monkeypatch.setattr(dp.repo, "signal_history", empty_history)
    monkeypatch.setattr(dp.repo, "recent_topics", empty_list)
    monkeypatch.setattr(dp.repo, "feedback_examples", empty_list)

    async def no_embedding(text: str) -> list[float]:
        raise RuntimeError("эмбеддинги в тесте не нужны")

    from src.scoring import novelty as novelty_mod

    monkeypatch.setattr(novelty_mod, "embed_one", no_embedding)


@pytest.fixture
def day_messages(monkeypatch: pytest.MonkeyPatch) -> list[Message]:
    messages = [
        msg(1, user=VASYA, text="Seedance режет 15-секундные ролики?"),
        msg(2, user=PETYA, reply_to=1, text="да, обрезает хвост", minutes=2),
        msg(3, user=MASHA, reply_to=1, text="ужимай референсы заранее", minutes=4),
        msg(4, user=VASYA, text="+", minutes=5),                       # шум
        msg(10, user=MASHA, text="кто идёт на конференцию?", minutes=90),
        msg(11, user=PETYA, reply_to=10, text="я иду", minutes=92),
        msg(12, user=VASYA, reply_to=10, text="и я", minutes=95),
    ]

    async def get_messages_by_day(chat_id: int, day, **kw: Any) -> list[Message]:
        return messages

    monkeypatch.setattr(dp.repo, "get_messages_by_day", get_messages_by_day)
    return messages


async def test_build_digest_end_to_end(day_messages):
    def answer(calls: list[dict[str, Any]]) -> Any:
        # последний вызов — это reduce
        if "Темы дня" in calls[-1]["messages"][1]["content"]:
            return {"highlights": ["Главное: ужимать референсы"]}
        n = len(calls)
        return {
            "title": f"Тема {n}",
            "decision": "решили так",
            "open_questions": ["остался вопрос?"],
            "links": [f"https://example.com/{n}"],
            "contributors": [{"tg_user_id": VASYA, "role": "initiator"}],
        }

    result = await dp.build_digest(chat(), date(2026, 9, 20), llm=fake_llm(answer))

    assert result.msg_count == 7, "в статистике учтены все сообщения, включая шум"
    assert len(result.topics) == 2, "два обсуждения, шумовая реплика не тема"
    assert "Главное: ужимать референсы" in result.markdown
    assert "❓ *Без ответа*" in result.markdown
    assert result.usage.tokens_in > 0


async def test_low_value_threads_never_reach_the_model(monkeypatch: pytest.MonkeyPatch):
    """Короткая перекличка не должна стоить денег."""
    async def get_messages_by_day(chat_id: int, day, **kw: Any) -> list[Message]:
        return [msg(1, user=VASYA, text="всем привет"), msg(2, user=PETYA, text="здорово")]

    monkeypatch.setattr(dp.repo, "get_messages_by_day", get_messages_by_day)
    llm = fake_llm({"title": "не должно вызваться"})

    result = await dp.build_digest(chat(), date(2026, 9, 20), llm=llm)

    assert llm.calls == []
    assert result.topics == []
    assert "ничего заметного" in result.markdown


async def test_empty_day(monkeypatch: pytest.MonkeyPatch):
    async def get_messages_by_day(chat_id: int, day, **kw: Any) -> list[Message]:
        return []

    monkeypatch.setattr(dp.repo, "get_messages_by_day", get_messages_by_day)
    result = await dp.build_digest(chat(), date(2026, 9, 20), llm=fake_llm({}))

    assert result.topics == []
    assert result.msg_count == 0


async def test_top_n_from_chat_settings(day_messages):
    llm = fake_llm(lambda calls: {"title": f"Тема {len(calls)}", "decision": "ага"})
    result = await dp.build_digest(
        chat(settings={"top_n": 1}), date(2026, 9, 20), llm=llm
    )
    assert len(result.topics) == 1, "порог берётся из настроек чата"


# ================================================ сбой модели не создаёт пустышку

async def test_total_llm_failure_is_not_saved_as_empty_digest(
    day_messages, monkeypatch: pytest.MonkeyPatch
):
    """Иначе за днём навсегда закрепится пустой дайджест из-за временного сбоя."""
    saved: list[Any] = []

    async def save_digest(*a: Any, **kw: Any) -> int:
        saved.append(a)
        return 1

    monkeypatch.setattr(dp.repo, "save_digest", save_digest)

    with pytest.raises(RuntimeError, match="не сохранён"):
        await dp.run_for_chat(
            chat(), date(2026, 9, 20), llm=fake_llm({}, fail=True)
        )
    assert saved == [], "в БД не должно попасть ничего"


async def test_partial_failure_still_produces_a_digest(day_messages):
    """Один упавший тред не отменяет остальные."""
    state = {"n": 0}

    async def flaky(messages, *, purpose: str, chat_id: int | None = None, **kw: Any):
        if purpose == "score":
            return GOOD_RUBRIC, Usage(80, 40, "t")
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("таймаут на первом треде")
        return {"title": f"Тема {state['n']}", "decision": "решили"}, Usage(50, 20, "t")

    result = await dp.build_digest(chat(), date(2026, 9, 20), llm=flaky)

    assert result.failed == 1
    assert result.mapped >= 1
    assert result.llm_is_down is False
    assert len(result.topics) >= 1


async def test_llm_is_down_only_when_everything_failed(day_messages):
    result = await dp.build_digest(chat(), date(2026, 9, 20), llm=fake_llm({}, fail=True))
    assert result.failed == 2 and result.mapped == 0
    assert result.llm_is_down is True
