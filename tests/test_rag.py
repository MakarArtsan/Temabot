"""Тесты RAG: чанкование, гибридный поиск, ответы (TZ шаг 8)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from src.db.models import Message
from src.nlp.chunk import chunk_thread, estimate_tokens
from src.nlp.embed import to_pgvector
from src.nlp.threads import segment
from src.rag import answer as rag_answer
from src.rag import search as rag_search
from src.rag.search import Hit, reciprocal_rank_fusion

START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
VASYA, PETYA = 101, 102


def msg(tg_msg_id: int, *, text: str = "текст", user: int = VASYA, minutes: int = 0,
        reply_to: int | None = None, **kw: Any) -> Message:
    return Message(
        chat_id=1, tg_msg_id=tg_msg_id, tg_user_id=user, author_name=f"user{user}",
        text=text, reply_to=reply_to, date=START + timedelta(minutes=minutes), **kw,
    )


def thread_of(messages: list[Message]):
    return segment(messages)[0]


# ============================================================== чанкование

def test_short_thread_is_one_chunk():
    thread = thread_of([
        msg(1, text="как ускорить рендер?"),
        msg(2, user=PETYA, reply_to=1, text="включи кэш", minutes=1),
    ])
    chunks = chunk_thread(thread)

    assert len(chunks) == 1
    assert chunks[0].msg_ids == [1, 2]
    assert "user101: как ускорить рендер?" in chunks[0].text
    assert chunks[0].thread_id == 1
    assert chunks[0].date_from == START


def test_long_thread_is_split_with_overlap():
    """Перехлёст нужен, чтобы ответ на границе окна не потерялся для поиска."""
    messages = [msg(n, text="длинная реплика " * 30, minutes=n) for n in range(1, 30)]
    chunks = chunk_thread(thread_of(messages), window_chars=2000, overlap_chars=250)

    assert len(chunks) > 1
    assert all(len(c.text) <= 3000 for c in chunks), "окно не разрастается"
    # соседние чанки должны делить хотя бы одно сообщение
    for first, second in pairwise(chunks):
        assert set(first.msg_ids) & set(second.msg_ids), "перехлёста нет"


def test_every_message_survives_chunking():
    messages = [msg(n, text=f"реплика номер {n} " * 20, minutes=n) for n in range(1, 25)]
    chunks = chunk_thread(thread_of(messages), window_chars=1500, overlap_chars=200)

    covered = {i for c in chunks for i in c.msg_ids}
    assert covered == {m.tg_msg_id for m in messages}


def test_voice_without_transcript_is_marked_not_dropped():
    thread = thread_of([msg(1, text=None, media_type="voice")])
    chunks = chunk_thread(thread)

    assert len(chunks) == 1 and "[voice]" in chunks[0].text


def test_empty_messages_are_skipped():
    thread = thread_of([msg(1, text=None), msg(2, text="настоящий текст", minutes=1)])
    chunks = chunk_thread(thread)

    assert chunks and chunks[0].msg_ids == [2]


def test_token_estimate_is_in_the_right_ballpark():
    assert 30 <= estimate_tokens("слово " * 25) <= 70


# ============================================================ вектор в строку

def test_pgvector_format():
    assert to_pgvector([0.5, -0.25, 0.0]) == "[0.500000,-0.250000,0.000000]"


# ==================================================================== RRF

def row(chunk_id: int, **kw: Any) -> dict[str, Any]:
    base = {
        "id": chunk_id, "chat_id": 1, "thread_id": chunk_id * 10,
        "msg_ids": [chunk_id], "text": f"чанк {chunk_id}",
        "date_from": START, "date_to": START,
    }
    base.update(kw)
    return base


def test_rrf_prefers_what_both_methods_found():
    """Смысл гибрида: то, что нашли оба способа, важнее лидера одного из них."""
    hits = reciprocal_rank_fusion({
        "vector": [row(1), row(2), row(3)],
        "fts": [row(3), row(4), row(1)],
    }, top=4)

    assert hits[0].thread_id in (10, 30)
    ids = [h.msg_ids[0] for h in hits]
    assert ids.index(3) < ids.index(2), "найденное обоими выше найденного одним"
    assert ids.index(1) < ids.index(2)


def test_rrf_marks_where_each_hit_came_from():
    hits = reciprocal_rank_fusion({"vector": [row(1)], "fts": [row(1), row(2)]}, top=2)
    by_id = {h.msg_ids[0]: h for h in hits}

    assert set(by_id[1].sources) == {"vector", "fts"}
    assert by_id[2].sources == ["fts"]


def test_rrf_respects_top():
    hits = reciprocal_rank_fusion(
        {"vector": [row(n) for n in range(1, 20)]}, top=8
    )
    assert len(hits) == 8


def test_rrf_on_empty_input():
    assert reciprocal_rank_fusion({"vector": [], "fts": []}) == []


def test_rrf_survives_one_missing_method():
    """Если эмбеддинги отвалились, поиск должен работать на полнотекстовом."""
    hits = reciprocal_rank_fusion({"fts": [row(1), row(2)]}, top=8)
    assert [h.msg_ids[0] for h in hits] == [1, 2]


async def test_hybrid_search_degrades_without_embeddings(monkeypatch: pytest.MonkeyPatch):
    async def broken_embed(text: str) -> list[float]:
        raise RuntimeError("модель эмбеддингов не загрузилась")

    async def by_text(query: str, **kw: Any) -> list[dict[str, Any]]:
        return [row(7)]

    async def by_vector(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        raise AssertionError("не должно вызваться")

    monkeypatch.setattr(rag_search, "embed_one", broken_embed)
    monkeypatch.setattr(rag_search.repo, "search_chunks_by_text", by_text)
    monkeypatch.setattr(rag_search.repo, "search_chunks_by_vector", by_vector)

    hits = await rag_search.hybrid_search("вопрос")

    assert len(hits) == 1 and hits[0].msg_ids == [7]


# ============================================================ сборка ответа

def a_hit(thread_id: int = 10, msg_ids: list[int] | None = None) -> Hit:
    return Hit(chat_id=1, thread_id=thread_id, msg_ids=msg_ids or [1], text="чанк")


async def test_context_expands_to_the_whole_thread(monkeypatch: pytest.MonkeyPatch):
    """Найденный кусок редко самодостаточен — нужен весь разговор."""
    thread = [msg(n, text=f"реплика {n}", minutes=n) for n in range(1, 6)]

    async def get_thread_messages(chat_id: int, thread_id: int, **kw: Any) -> list[Message]:
        return thread

    monkeypatch.setattr(rag_answer.repo, "get_thread_messages", get_thread_messages)

    context, used = await rag_answer.build_context([a_hit()])

    assert len(used) == 5
    assert "[3] user101: реплика 3" in context


async def test_context_falls_back_to_neighbours(monkeypatch: pytest.MonkeyPatch):
    """Если тред ещё не размечен, берём соседей по времени."""
    async def no_thread(chat_id: int, thread_id: int, **kw: Any) -> list[Message]:
        return []

    async def around(chat_id: int, tg_msg_id: int, **kw: Any) -> list[Message]:
        return [msg(5, text="сосед")]

    monkeypatch.setattr(rag_answer.repo, "get_thread_messages", no_thread)
    monkeypatch.setattr(rag_answer.repo, "get_messages_around", around)

    context, used = await rag_answer.build_context([a_hit()])

    assert len(used) == 1 and "сосед" in context


async def test_context_does_not_repeat_messages(monkeypatch: pytest.MonkeyPatch):
    """Два попадания в один тред не должны удваивать контекст и деньги."""
    thread = [msg(n, minutes=n) for n in range(1, 4)]

    async def get_thread_messages(chat_id: int, thread_id: int, **kw: Any) -> list[Message]:
        return thread

    monkeypatch.setattr(rag_answer.repo, "get_thread_messages", get_thread_messages)

    _, used = await rag_answer.build_context([a_hit(10, [1]), a_hit(10, [2])])

    assert len(used) == 3


async def test_context_is_capped(monkeypatch: pytest.MonkeyPatch):
    """Иначе один длинный тред съест весь бюджет запроса."""
    huge = [msg(n, text="очень длинная реплика " * 100, minutes=n) for n in range(1, 60)]

    async def get_thread_messages(chat_id: int, thread_id: int, **kw: Any) -> list[Message]:
        return huge

    monkeypatch.setattr(rag_answer.repo, "get_thread_messages", get_thread_messages)

    context, used = await rag_answer.build_context([a_hit()])

    assert len(context) <= rag_answer.MAX_CONTEXT_CHARS
    assert len(used) < len(huge)


async def test_no_hits_means_honest_answer(monkeypatch: pytest.MonkeyPatch):
    """Выдуманный ответ хуже отсутствия ответа."""
    async def nothing(*a: Any, **kw: Any) -> list[Hit]:
        return []

    monkeypatch.setattr(rag_answer, "hybrid_search", nothing)

    answer = await rag_answer.answer_question("что там про Seedance?")

    assert answer.found is False
    assert "нет" in answer.text.lower()
    assert answer.sources == []


async def test_answer_collects_sources(monkeypatch: pytest.MonkeyPatch):
    from src.db.models import Chat
    from src.llm.client import LLMReply, Usage

    thread = [msg(n, text=f"реплика {n}", minutes=n) for n in range(1, 4)]

    async def hits(*a: Any, **kw: Any) -> list[Hit]:
        return [a_hit(10, [2])]

    async def get_thread_messages(*a: Any, **kw: Any) -> list[Message]:
        return thread

    async def get_chat_by_id(chat_id: int) -> Chat:
        return Chat(id=1, tg_id=-1002354231333, title="Группа")

    async def fake_chat(messages: list[dict[str, str]], **kw: Any) -> LLMReply:
        return LLMReply(text="Ответ по контексту [2].", usage=Usage(100, 20, "m"))

    logged: list[Any] = []

    async def log_qa(question: str, answer: str, sources: list[int]) -> None:
        logged.append((question, answer, sources))

    monkeypatch.setattr(rag_answer, "hybrid_search", hits)
    monkeypatch.setattr(rag_answer.repo, "get_thread_messages", get_thread_messages)
    monkeypatch.setattr(rag_answer.repo, "get_chat_by_id", get_chat_by_id)
    monkeypatch.setattr(rag_answer.repo, "log_qa", log_qa)
    monkeypatch.setattr(rag_answer, "chat", fake_chat)

    answer = await rag_answer.answer_question("вопрос")

    assert answer.text == "Ответ по контексту [2]."
    assert len(answer.sources) == 1
    assert answer.sources[0].tg_msg_id == 2
    assert "https://t.me/c/2354231333/2" in answer.as_html()
    assert "<b>Источники</b>" in answer.as_html()
    assert logged and logged[0][2] == [2]


async def test_answer_escapes_model_output(monkeypatch: pytest.MonkeyPatch):
    from src.llm.client import LLMReply, Usage

    async def hits(*a: Any, **kw: Any) -> list[Hit]:
        return [a_hit()]

    async def get_thread_messages(*a: Any, **kw: Any) -> list[Message]:
        return [msg(1)]

    async def fake_chat(*a: Any, **kw: Any) -> LLMReply:
        return LLMReply(text="<script>alert(1)</script> & цена < 100", usage=Usage())

    async def log_qa(*a: Any, **kw: Any) -> None:
        return None

    async def get_chat_by_id(chat_id: int) -> Any:
        from src.db.models import Chat

        return Chat(id=1, tg_id=-1002354231333, title="Группа")

    monkeypatch.setattr(rag_answer, "hybrid_search", hits)
    monkeypatch.setattr(rag_answer.repo, "get_thread_messages", get_thread_messages)
    monkeypatch.setattr(rag_answer.repo, "get_chat_by_id", get_chat_by_id)
    monkeypatch.setattr(rag_answer.repo, "log_qa", log_qa)
    monkeypatch.setattr(rag_answer, "chat", fake_chat)

    html = (await rag_answer.answer_question("вопрос")).as_html()

    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "&amp;" in html
