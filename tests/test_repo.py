"""Тесты доступа к БД (TZ шаг 2). Требуют Postgres с pgvector — см. tests/conftest.py."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from src.db import repo
from src.db.models import Message

KAMCHATKA = "Asia/Kamchatka"
pytestmark = pytest.mark.usefixtures("db")


def _msg(chat_id: int, tg_msg_id: int, **kw) -> Message:
    defaults = dict(
        tg_user_id=1001,
        author_name="Вася",
        text=f"сообщение {tg_msg_id}",
        date=datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo(KAMCHATKA)),
    )
    defaults.update(kw)
    return Message(chat_id=chat_id, tg_msg_id=tg_msg_id, **defaults)


async def _chat(tg_id: int = -1001234567890) -> int:
    chat = await repo.get_or_create_chat(tg_id, "Закрытая группа")
    return chat.id


# ---------------------------------------------------------------------- чаты

async def test_get_or_create_chat_is_idempotent_and_starts_disabled():
    first = await repo.get_or_create_chat(-100111, "Группа")
    second = await repo.get_or_create_chat(-100111, "Группа переименована")

    assert first.id == second.id, "повторный вызов не должен плодить чаты"
    assert second.title == "Группа переименована"
    # новые чаты появляются выключенными, включает владелец руками (TZ §4.8)
    assert (second.collect, second.digest, second.copier) == (False, False, "ask")


async def test_list_chats_filters_by_collect():
    await repo.get_or_create_chat(-100111, "Собираем")
    await repo.get_or_create_chat(-100222, "Не собираем")
    await repo.set_chat_flags(-100111, collect=True)

    collected = await repo.list_chats(collect=True)
    assert [c.tg_id for c in collected] == [-100111]


async def test_update_chat_settings_merges_patch():
    chat_id = await _chat()
    await repo.update_chat_settings(chat_id, {"weights": {"w_use": 1.0}, "top_n": 6})
    merged = await repo.update_chat_settings(chat_id, {"top_n": 4})

    assert merged["top_n"] == 4
    assert merged["weights"] == {"w_use": 1.0}, "патч не должен затирать соседние ключи"


# ------------------------------------------------------------------ сообщения

async def test_upsert_message_inserts_then_updates_same_row():
    chat_id = await _chat()
    first_id = await repo.upsert_message(_msg(chat_id, 5))
    second_id = await repo.upsert_message(_msg(chat_id, 5, text="поправленный текст"))

    assert first_id == second_id
    assert await repo.count_messages(chat_id) == 1
    stored = await repo.get_message(chat_id, 5)
    assert stored is not None and stored.text == "поправленный текст"


async def test_upsert_does_not_wipe_fields_set_by_other_processes():
    """Бэкфилл поверх собранного не должен стирать расшифровку и пометки копировщика."""
    chat_id = await _chat()
    await repo.upsert_message(
        _msg(chat_id, 7, transcript="расшифровка", is_pinned_by_me=True, copy_count=2)
    )
    await repo.set_thread_id(chat_id, [7], 42)

    await repo.upsert_message(_msg(chat_id, 7))  # как будто прилетел бэкфилл

    stored = await repo.get_message(chat_id, 7)
    assert stored is not None
    assert stored.transcript == "расшифровка"
    assert stored.is_pinned_by_me is True
    assert stored.copy_count == 2
    assert stored.thread_id == 42


async def test_message_content_joins_text_and_transcript():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 8, text="текст", transcript="голос"))
    stored = await repo.get_message(chat_id, 8)

    assert stored is not None and stored.content == "текст\nголос"


async def test_edit_and_soft_delete():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 9))

    assert await repo.update_message_text(chat_id, 9, "новый текст") is True
    edited = await repo.get_message(chat_id, 9)
    assert edited is not None and edited.text == "новый текст" and edited.edited_at is not None

    assert await repo.soft_delete_messages(chat_id, [9]) == 1
    deleted = await repo.get_message(chat_id, 9)
    assert deleted is not None and deleted.is_deleted, "удаление мягкое, строка остаётся"
    # повторное удаление ничего не меняет
    assert await repo.soft_delete_messages(chat_id, [9]) == 0


async def test_deleted_message_still_counts_for_the_day():
    """TZ §4.1: дайджест за день должен остаться честным."""
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 10))
    await repo.soft_delete_messages(chat_id, [10])

    assert len(await repo.get_messages_by_day(chat_id, date(2026, 9, 20))) == 1
    assert (
        await repo.get_messages_by_day(chat_id, date(2026, 9, 20), include_deleted=False) == []
    )


async def test_get_messages_by_day_uses_local_timezone():
    """Сутки считаются по Asia/Kamchatka (UTC+12), а не по UTC."""
    chat_id = await _chat()
    kam = ZoneInfo(KAMCHATKA)
    # 20 сентября 23:30 по Камчатке = 11:30 UTC того же дня
    await repo.upsert_message(_msg(chat_id, 20, date=datetime(2026, 9, 20, 23, 30, tzinfo=kam)))
    # 21 сентября 00:30 по Камчатке = 12:30 UTC 20-го — в UTC это ещё «вчера»
    await repo.upsert_message(_msg(chat_id, 21, date=datetime(2026, 9, 21, 0, 30, tzinfo=kam)))

    day20 = await repo.get_messages_by_day(chat_id, date(2026, 9, 20), tz=KAMCHATKA)
    day21 = await repo.get_messages_by_day(chat_id, date(2026, 9, 21), tz=KAMCHATKA)

    assert [m.tg_msg_id for m in day20] == [20]
    assert [m.tg_msg_id for m in day21] == [21]


async def test_messages_are_ordered_by_date():
    chat_id = await _chat()
    base = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    for n, shift in ((3, 20), (1, 0), (2, 10)):
        await repo.upsert_message(_msg(chat_id, n, date=base + timedelta(minutes=shift)))

    day = await repo.get_messages_by_day(chat_id, date(2026, 9, 20), tz="UTC")
    assert [m.tg_msg_id for m in day] == [1, 2, 3]


async def test_messages_of_other_chats_do_not_leak():
    chat_a = await _chat(-100111)
    chat_b = await _chat(-100222)
    await repo.upsert_message(_msg(chat_a, 1))
    await repo.upsert_message(_msg(chat_b, 1))  # тот же tg_msg_id в другом чате

    assert await repo.count_messages(chat_a) == 1
    assert await repo.count_messages() == 2
    day = await repo.get_messages_by_day(chat_a, date(2026, 9, 20))
    assert len(day) == 1 and day[0].chat_id == chat_a


async def test_get_last_tg_msg_id_for_resume():
    chat_id = await _chat()
    assert await repo.get_last_tg_msg_id(chat_id) is None
    for n in (4, 17, 9):
        await repo.upsert_message(_msg(chat_id, n))

    assert await repo.get_last_tg_msg_id(chat_id) == 17


async def test_set_transcript():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 11, text=None, media_type="voice"))

    assert await repo.set_transcript(chat_id, 11, "привет из голосового") is True
    stored = await repo.get_message(chat_id, 11)
    assert stored is not None and stored.transcript == "привет из голосового"


# --------------------------------------------------------------------- авторы

async def test_upsert_author_keeps_name_when_not_provided():
    await repo.upsert_author(500, "Вася")
    again = await repo.upsert_author(500, None)

    assert again.name == "Вася"
    assert again.weight == pytest.approx(1.0)
    assert again.muted is False


# ---------------------------------------------------------------------- state

async def test_state_roundtrip():
    assert await repo.get_state("last_msg_id", default=0) == 0

    await repo.set_state("last_msg_id", {"-100111": 42})
    assert await repo.get_state("last_msg_id") == {"-100111": 42}

    await repo.set_state("last_msg_id", {"-100111": 43})
    assert await repo.get_state("last_msg_id") == {"-100111": 43}


# ------------------------------------------------------------------ дайджесты

async def test_save_digest_is_idempotent_per_day():
    """TZ §4.3 п.8: повторный запуск за тот же день перезаписывает, а не дублирует."""
    chat_id = await _chat()
    day = date(2026, 9, 20)
    first = await repo.save_digest(chat_id, day, "первая версия", msg_count=10)
    second = await repo.save_digest(
        chat_id, day, "вторая версия",
        payload={"topics": [{"title": "Seedance"}], "highlights": ["главное"]},
        msg_count=12,
    )

    assert first == second
    stored = await repo.get_digest(chat_id, day)
    assert stored is not None
    assert stored.summary_md == "вторая версия"
    assert stored.topics == [{"title": "Seedance"}]
    assert stored.payload["highlights"] == ["главное"], "структура дайджеста цела"
    assert stored.msg_count == 12


async def test_digests_of_different_days_coexist():
    chat_id = await _chat()
    await repo.save_digest(chat_id, date(2026, 9, 20), "двадцатое")
    await repo.save_digest(chat_id, date(2026, 9, 21), "двадцать первое")

    first = await repo.get_digest(chat_id, date(2026, 9, 20))
    second = await repo.get_digest(chat_id, date(2026, 9, 21))
    assert first is not None and second is not None and first.id != second.id


async def test_log_llm_usage():
    chat_id = await _chat()
    await repo.log_llm_usage(
        chat_id=chat_id, purpose="summary", model="glm-5.3-flash",
        tokens_in=1200, tokens_out=300, cost_usd=0.0021,
    )
    from src.db import pool

    total = await pool.fetchval("select count(*) from llm_usage where chat_id = $1", chat_id)
    assert total == 1


# ------------------------------------------------- запросы для команд бота

async def test_search_finds_by_russian_morphology():
    """Полнотекстовый поиск должен понимать склонения."""
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 1, text="обсуждали цены на подписку"))
    await repo.upsert_message(_msg(chat_id, 2, text="совсем про другое"))

    found = await repo.search_messages("цена подписки")
    assert [m.tg_msg_id for m in found] == [1]


async def test_search_looks_into_voice_transcripts():
    """Для читателя расшифровка голосового — такой же текст."""
    chat_id = await _chat()
    await repo.upsert_message(
        _msg(chat_id, 3, text=None, media_type="voice", transcript="говорю про Seedance")
    )
    assert [m.tg_msg_id for m in await repo.search_messages("Seedance")] == [3]


async def test_search_skips_deleted():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 4, text="удалённое про Seedance"))
    await repo.soft_delete_messages(chat_id, [4])

    assert await repo.search_messages("Seedance") == []


async def test_search_respects_limit():
    chat_id = await _chat()
    for n in range(10, 20):
        await repo.upsert_message(_msg(chat_id, n, text="одинаковый текст про рендер"))

    assert len(await repo.search_messages("рендер", limit=3)) == 3


async def test_author_activity():
    """Этот запрос падал на приведении типов — проверяем его на живой базе."""
    chat_id = await _chat()
    for n in range(5):
        await repo.upsert_message(_msg(chat_id, n + 1, text=f"сообщение {n}"))
    await repo.upsert_message(_msg(chat_id, 99, tg_user_id=2002, author_name="Петя"))

    found = await repo.get_author_activity("Вася")
    assert found is not None
    assert found["messages"] == 5
    assert found["name"] == "Вася"

    assert await repo.get_author_activity("Такого нет") is None


async def test_author_activity_by_user_id():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 1))
    found = await repo.get_author_activity("1001")
    assert found is not None and found["tg_user_id"] == 1001


async def test_collection_stats():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 1))
    await repo.upsert_message(
        _msg(chat_id, 2, media_type="voice", text=None, transcript="расшифровка")
    )
    await repo.log_llm_usage(
        chat_id=chat_id, purpose="summary", model="m", tokens_in=100, tokens_out=50
    )
    await repo.save_digest(chat_id, date(2026, 9, 20), "текст")

    stats = await repo.get_collection_stats()

    assert stats["messages"] == 2
    assert stats["voices"] == 1
    assert stats["transcribed"] == 1
    assert stats["tokens_in"] == 100 and stats["tokens_out"] == 50
    assert stats["llm_calls"] == 1
    assert stats["digests"] == 1
    assert stats["last_at"] is not None


async def test_list_digests_covers_the_week():
    chat_id = await _chat()
    for day in range(14, 22):
        await repo.save_digest(chat_id, date(2026, 9, day), f"день {day}")

    week = await repo.list_digests(chat_id, days=7, until=date(2026, 9, 21))

    assert [d.day.day for d in week] == [15, 16, 17, 18, 19, 20, 21]


async def test_set_pinned():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 1))

    assert await repo.set_pinned(chat_id, 1) is True
    stored = await repo.get_message(chat_id, 1)
    assert stored is not None and stored.is_pinned_by_me is True
    assert await repo.set_pinned(chat_id, 404) is False


async def test_pending_transcriptions():
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 1, media_type="voice", text=None))
    await repo.upsert_message(
        _msg(chat_id, 2, media_type="voice", text=None, transcript="уже есть")
    )
    await repo.upsert_message(_msg(chat_id, 3, text="обычный текст"))

    pending = await repo.get_pending_transcriptions(chat_id)
    assert [m.tg_msg_id for m in pending] == [1]


# ------------------------------------------------------------- чанки и RAG

def _vec(seed: float) -> str:
    """Вектор нужной размерности: один «горячий» компонент задаёт направление."""
    values = [0.0] * 1024
    values[int(seed) % 1024] = 1.0
    return "[" + ",".join(f"{v:.3f}" for v in values) + "]"


async def test_replace_chunks_does_not_duplicate():
    """Тред дополняется, переиндексация не должна плодить копии в поиске."""
    chat_id = await _chat()
    rows = [{"msg_ids": [1, 2], "text": "первая версия", "embedding": _vec(1)}]
    await repo.replace_chunks(chat_id, 100, rows)
    await repo.replace_chunks(
        chat_id, 100, [{"msg_ids": [1, 2, 3], "text": "вторая версия", "embedding": _vec(1)}]
    )

    from src.db import pool

    total = await pool.fetchval("select count(*) from chunks where thread_id = 100")
    text = await pool.fetchval("select text from chunks where thread_id = 100")
    assert total == 1 and text == "вторая версия"


async def test_chunk_text_search_matches_partial_question():
    """Вопрос «сколько стоит генерация ролика» должен находить «12 рублей за ролик»."""
    chat_id = await _chat()
    await repo.replace_chunks(chat_id, 1, [{
        "msg_ids": [1], "text": "Петя: примерно 12 рублей за ролик на тарифе про",
        "embedding": _vec(1),
    }])
    await repo.replace_chunks(chat_id, 2, [{
        "msg_ids": [2], "text": "Маша: конференция будет 14 ноября в Москве",
        "embedding": _vec(2),
    }])

    found = await repo.search_chunks_by_text("сколько стоит генерация ролика")

    assert found, "поиск по всем словам сразу ничего бы не нашёл"
    assert found[0]["thread_id"] == 1


async def test_chunk_text_search_ranks_by_overlap():
    chat_id = await _chat()
    await repo.replace_chunks(chat_id, 1, [{
        "msg_ids": [1], "text": "конференция по нейросетям в ноябре, билет 30 тысяч",
        "embedding": _vec(1),
    }])
    await repo.replace_chunks(chat_id, 2, [{
        "msg_ids": [2], "text": "купил билет на поезд", "embedding": _vec(2),
    }])

    found = await repo.search_chunks_by_text("когда конференция и сколько билет")
    assert found[0]["thread_id"] == 1


async def test_chunk_vector_search_orders_by_closeness():
    chat_id = await _chat()
    for thread_id, seed in ((1, 5), (2, 500), (3, 900)):
        await repo.replace_chunks(chat_id, thread_id, [{
            "msg_ids": [thread_id], "text": f"тред {thread_id}", "embedding": _vec(seed),
        }])

    found = await repo.search_chunks_by_vector(_vec(500), limit=3)

    assert found[0]["thread_id"] == 2, "ближайший вектор должен быть первым"
    assert found[0]["score"] > found[1]["score"]


async def test_days_with_unassigned_messages():
    """Разметка идёт от данных: после бэкфилла старые дни тоже должны попасть."""
    chat_id = await _chat()
    kam = ZoneInfo(KAMCHATKA)
    await repo.upsert_message(_msg(chat_id, 1, date=datetime(2026, 8, 1, 12, tzinfo=kam)))
    await repo.upsert_message(_msg(chat_id, 2, date=datetime(2026, 9, 20, 12, tzinfo=kam)))

    days = await repo.get_days_with_unassigned_messages(chat_id, tz=KAMCHATKA)
    assert [str(d) for d in days] == ["2026-09-20", "2026-08-01"]

    await repo.set_thread_id(chat_id, [1, 2], 1)
    assert await repo.get_days_with_unassigned_messages(chat_id, tz=KAMCHATKA) == []


async def test_unindexed_threads_include_the_ones_that_grew():
    chat_id = await _chat()
    kam = ZoneInfo(KAMCHATKA)
    await repo.upsert_message(_msg(chat_id, 1, date=datetime(2026, 9, 20, 12, tzinfo=kam)))
    await repo.set_thread_id(chat_id, [1], 1)

    assert await repo.get_unindexed_threads(chat_id) == [1]

    await repo.replace_chunks(chat_id, 1, [{
        "msg_ids": [1], "text": "тред", "embedding": _vec(1),
        "date_from": datetime(2026, 9, 20, 12, tzinfo=kam),
        "date_to": datetime(2026, 9, 20, 12, tzinfo=kam),
    }])
    assert await repo.get_unindexed_threads(chat_id) == [], "уже проиндексирован"

    # в тред дописали новое сообщение — он снова требует индексации
    await repo.upsert_message(_msg(chat_id, 2, date=datetime(2026, 9, 20, 13, tzinfo=kam)))
    await repo.set_thread_id(chat_id, [2], 1)
    assert await repo.get_unindexed_threads(chat_id) == [1]


async def test_thread_messages_and_neighbours():
    chat_id = await _chat()
    kam = ZoneInfo(KAMCHATKA)
    for n in range(1, 8):
        await repo.upsert_message(
            _msg(chat_id, n, date=datetime(2026, 9, 20, 12, n, tzinfo=kam))
        )
    await repo.set_thread_id(chat_id, [2, 3, 4], 2)

    thread = await repo.get_thread_messages(chat_id, 2)
    assert [m.tg_msg_id for m in thread] == [2, 3, 4]

    around = await repo.get_messages_around(chat_id, 4, radius=2)
    assert [m.tg_msg_id for m in around] == [2, 3, 4, 5, 6]


async def test_qa_log():
    await repo.log_qa("вопрос", "ответ", [1, 2])
    from src.db import pool

    row = await pool.fetchrow("select * from qa_log")
    assert row["question"] == "вопрос" and row["sources"] == [1, 2]


# ------------------------------------------------------- копировщик (§4.6)

async def test_mark_copied_does_not_duplicate_collector_record():
    """Коллектор уже сохранил сообщение — копирование только помечает его."""
    chat_id = await _chat()
    await repo.upsert_message(_msg(chat_id, 5, text="оригинальный текст коллектора"))

    existed = await repo.mark_copied(
        chat_tg_id=-1001234567890, tg_msg_id=5, text="текст из копировщика",
        tg_user_id=1001, author_name="Вася",
        date=datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo(KAMCHATKA)),
    )

    assert existed is True
    assert await repo.count_messages(chat_id) == 1, "дубля быть не должно"

    stored = await repo.get_message(chat_id, 5)
    assert stored is not None
    assert stored.is_pinned_by_me is True
    assert stored.copy_count == 1
    assert stored.source == "collector", "источник не меняется (TZ §4.6)"
    assert stored.text == "оригинальный текст коллектора", "текст коллектора полнее"


async def test_mark_copied_inserts_when_collector_has_not_seen_it():
    """Бот может работать в группе, которую userbot не читает."""
    chat_id = await _chat()
    existed = await repo.mark_copied(
        chat_tg_id=-1001234567890, tg_msg_id=77, text="текст из копировщика",
        tg_user_id=2002, author_name="Петя",
        date=datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo(KAMCHATKA)),
    )

    assert existed is False
    stored = await repo.get_message(chat_id, 77)
    assert stored is not None
    assert stored.source == "copier"
    assert stored.text == "текст из копировщика"
    assert stored.is_pinned_by_me is True
    assert stored.copy_count == 1


async def test_mark_copied_counts_repeats():
    chat_id = await _chat()
    date_ = datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo(KAMCHATKA))
    for _ in range(3):
        await repo.mark_copied(
            chat_tg_id=-1001234567890, tg_msg_id=9, text="текст",
            tg_user_id=1001, author_name="Вася", date=date_,
        )

    stored = await repo.get_message(chat_id, 9)
    assert stored is not None and stored.copy_count == 3


async def test_mark_copied_creates_chat_and_author():
    """Группа могла быть ещё неизвестна — она появится выключенной (§4.8)."""
    await repo.mark_copied(
        chat_tg_id=-100555, tg_msg_id=1, text="текст", tg_user_id=3003,
        author_name="Новичок",
        date=datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo(KAMCHATKA)),
    )

    chat = await repo.get_chat_by_tg_id(-100555)
    assert chat is not None
    assert (chat.collect, chat.digest, chat.copier) == (False, False, "ask")

    from src.db import pool

    author = await pool.fetchval("select name from authors where tg_user_id = 3003")
    assert author == "Новичок"


async def test_mark_copied_survives_missing_author():
    """У сообщений от имени канала автора нет."""
    chat_id = await _chat()
    await repo.mark_copied(
        chat_tg_id=-1001234567890, tg_msg_id=11, text="пост канала",
        tg_user_id=None, author_name=None,
        date=datetime(2026, 9, 20, 12, 0, tzinfo=ZoneInfo(KAMCHATKA)),
    )
    stored = await repo.get_message(chat_id, 11)
    assert stored is not None and stored.tg_user_id is None


# ------------------------------------------------- доступ и блок-лист (§4.8)

async def test_blocklist_roundtrip():
    await repo.upsert_author(500, "Флудер")
    await repo.block_user(500, reason="флудит")

    assert await repo.blocked_user_ids() == {500}
    blocked = await repo.list_blocked()
    assert blocked[0]["name"] == "Флудер" and blocked[0]["reason"] == "флудит"

    assert await repo.unblock_user(500) is True
    assert await repo.blocked_user_ids() == set()
    assert await repo.unblock_user(500) is False, "второй раз снимать нечего"


async def test_block_user_twice_updates_reason():
    await repo.block_user(500, reason="первая причина")
    await repo.block_user(500, reason="вторая причина")

    blocked = await repo.list_blocked()
    assert len(blocked) == 1 and blocked[0]["reason"] == "вторая причина"


async def test_find_author_by_id_name_and_username():
    await repo.upsert_author(1001, "Вася Пупкин")
    await repo.upsert_author(1002, "@petya")

    by_id = await repo.find_author("1001")
    by_name = await repo.find_author("пупкин")
    by_username = await repo.find_author("@petya")

    assert by_id is not None and by_id.tg_user_id == 1001
    assert by_name is not None and by_name.tg_user_id == 1001
    assert by_username is not None and by_username.tg_user_id == 1002
    assert await repo.find_author("такого нет") is None


async def test_find_chats_by_title_and_id():
    await repo.get_or_create_chat(-100111, "Рабочая группа")
    await repo.get_or_create_chat(-100222, "Домашний чат")

    by_title = await repo.find_chats("рабочая")
    by_id = await repo.find_chats("-100222")

    assert [c.tg_id for c in by_title] == [-100111]
    assert [c.tg_id for c in by_id] == [-100222]
    assert await repo.find_chats("несуществующая") == []


async def test_set_chat_flags_touches_only_what_is_given():
    await repo.get_or_create_chat(-100111, "Группа")
    await repo.set_chat_flags(-100111, collect=True, digest=True, copier="allow")

    updated = await repo.set_chat_flags(-100111, copier="deny")

    assert updated is not None
    assert updated.copier == "deny"
    assert updated.collect is True and updated.digest is True, "остальное не трогаем"


async def test_set_chat_flags_rejects_unknown_copier_mode():
    """Схема защищена check-констрейнтом: опечатка не должна тихо записаться."""
    import asyncpg

    await repo.get_or_create_chat(-100111, "Группа")
    with pytest.raises(asyncpg.IntegrityConstraintViolationError):
        await repo.set_chat_flags(-100111, copier="maybe")


# ------------------------------------------------------- рейтинги (§4.10)

async def test_author_stats_roundtrip_is_idempotent():
    """Пересчёт дня заменяет статистику, а не накапливает её."""
    chat_id = await _chat()
    day = date(2026, 9, 20)
    row = {
        "tg_user_id": 1001, "messages": 5, "short_msgs": 1, "words": 40,
        "longest_msg": 12, "voice_sec": 60, "links": 2, "replies_got": 3,
        "reactions_got": 4, "questions_answered": 1, "threads_started": 1,
        "night_msgs": 0, "usefulness": 0.75,
    }
    await repo.replace_author_stats(chat_id, day, [row])
    await repo.replace_author_stats(chat_id, day, [{**row, "messages": 7}])

    stats = await repo.get_author_stats(chat_id, date_from=day, date_to=day)
    assert len(stats) == 1
    assert stats[0]["messages"] == 7


async def test_week_is_the_sum_of_days():
    """Неделя и месяц не хранятся отдельно — это сумма по дням (§4.10)."""
    chat_id = await _chat()
    for offset in range(3):
        day = date(2026, 9, 18 + offset)
        await repo.replace_author_stats(chat_id, day, [{
            "tg_user_id": 1001, "messages": 5, "short_msgs": 0, "words": 10,
            "longest_msg": 4, "voice_sec": 30, "links": 1, "replies_got": 1,
            "reactions_got": 1, "questions_answered": 0, "threads_started": 0,
            "night_msgs": 0, "usefulness": 0.5,
        }])

    week = await repo.get_author_stats(
        chat_id, date_from=date(2026, 9, 18), date_to=date(2026, 9, 20)
    )
    assert week[0]["messages"] == 15
    assert week[0]["voice_sec"] == 90
    assert float(week[0]["usefulness"]) == pytest.approx(1.5)


async def test_optout_hides_from_ratings():
    """Участник с /optout не появляется ни в одном рейтинге (§4.10)."""
    chat_id = await _chat()
    day = date(2026, 9, 20)
    await repo.upsert_author(1001, "Вася")
    await repo.upsert_author(2002, "Петя")
    await repo.replace_author_stats(chat_id, day, [
        {"tg_user_id": u, "messages": 5, "short_msgs": 0, "words": 10, "longest_msg": 4,
         "voice_sec": 0, "links": 0, "replies_got": 0, "reactions_got": 0,
         "questions_answered": 0, "threads_started": 0, "night_msgs": 0,
         "usefulness": 0.5}
        for u in (1001, 2002)
    ])

    await repo.set_hide_from_ratings(2002, True)

    visible = await repo.get_author_stats(chat_id, date_from=day, date_to=day)
    assert [int(r["tg_user_id"]) for r in visible] == [1001]

    # в админке скрытые всё же видны
    everyone = await repo.get_author_stats(
        chat_id, date_from=day, date_to=day, hide_optout=False
    )
    assert len(everyone) == 2

    await repo.set_hide_from_ratings(2002, False)
    assert len(await repo.get_author_stats(chat_id, date_from=day, date_to=day)) == 2


async def test_thread_contributions_join_digest_scores():
    """Формуле полезности нужен скор темы — он приходит из digest_items."""
    chat_id = await _chat()
    day = date(2026, 9, 20)
    digest_id = await repo.save_digest(chat_id, day, "текст", payload={})
    await repo.save_digest_items(digest_id, chat_id, [
        {"thread_id": 7, "title": "Тема", "kind": "insight", "score": 0.8, "shown": True,
         "features": {}},
    ])
    await repo.save_thread_contributions(chat_id, day, [
        {"thread_id": 7, "tg_user_id": 1001, "role": "key"},
    ])

    rows = await repo.get_thread_contributions(chat_id, day)
    assert len(rows) == 1
    assert float(rows[0]["score"]) == pytest.approx(0.8)
    assert rows[0]["shown"] is True


async def test_thread_contributions_are_replaced_not_stacked():
    chat_id = await _chat()
    day = date(2026, 9, 20)
    await repo.save_thread_contributions(chat_id, day, [
        {"thread_id": 7, "tg_user_id": 1001, "role": "key"},
    ])
    await repo.save_thread_contributions(chat_id, day, [
        {"thread_id": 7, "tg_user_id": 1001, "role": "initiator"},
    ])

    rows = await repo.get_thread_contributions(chat_id, day)
    assert [r["role"] for r in rows] == ["initiator"]


# ------------------------------------------------------ первый запуск и индекс

async def test_bootstrap_primary_chat_turns_everything_on_for_a_new_group():
    """После деплоя копировщик в основной группе не должен замолчать."""
    chat = await repo.bootstrap_primary_chat(-100555)

    assert chat.collect and chat.digest and chat.copier == "allow"


async def test_bootstrap_primary_chat_keeps_owner_choices():
    """Рестарт не должен включать обратно то, что владелец выключил в админке."""
    await repo.bootstrap_primary_chat(-100555)
    await repo.set_chat_flags(-100555, digest=False, copier="deny")

    chat = await repo.bootstrap_primary_chat(-100555)

    assert chat.collect and not chat.digest and chat.copier == "deny"


async def test_threads_without_embeddings():
    chat_id = await _chat()
    await repo.replace_chunks(chat_id, 1, [{"msg_ids": [1], "text": "а", "embedding": None}])
    await repo.replace_chunks(chat_id, 2, [{"msg_ids": [2], "text": "б", "embedding": _vec(2)}])

    assert await repo.get_threads_without_embeddings(chat_id) == [1]


async def _indexable_chat() -> tuple[object, int]:
    chat = await repo.get_or_create_chat(-100777, "Группа")
    kam = ZoneInfo(KAMCHATKA)
    await repo.upsert_message(_msg(
        chat.id, 1, text="сколько стоит генерация ролика?",
        date=datetime(2026, 9, 20, 12, tzinfo=kam),
    ))
    await repo.upsert_message(_msg(
        chat.id, 2, text="примерно 12 рублей за ролик", reply_to=1,
        date=datetime(2026, 9, 20, 12, 1, tzinfo=kam),
    ))
    return chat, chat.id


async def test_index_without_embeddings_still_feeds_text_search(monkeypatch):
    """В образе нет sentence-transformers — /ask всё равно должен находить историю."""
    from src.rag import index as rag_index

    async def broken(texts, **kw):
        raise RuntimeError("Не установлен sentence-transformers")

    monkeypatch.setattr(rag_index, "embed_texts", broken)
    chat, chat_id = await _indexable_chat()

    result = await rag_index.index_chat(chat)

    assert result.chunks >= 1 and result.without_vectors == result.threads
    found = await repo.search_chunks_by_text("сколько стоит ролик")
    assert found, "без векторов чанки должны попасть в полнотекстовый поиск"
    assert await repo.get_unindexed_threads(chat_id) == [], "повторно не индексируем"


async def test_vectors_are_added_when_embeddings_come_back(monkeypatch):
    from src.db import pool
    from src.rag import index as rag_index

    async def broken(texts, **kw):
        raise RuntimeError("нет модели")

    calls: list[int] = []

    async def working(texts, **kw):
        calls.append(len(texts))
        return [[0.1] * 1024 for _ in texts]

    chat, chat_id = await _indexable_chat()
    monkeypatch.setattr(rag_index, "embed_texts", broken)
    await rag_index.index_chat(chat)

    monkeypatch.setattr(rag_index, "embed_texts", working)
    result = await rag_index.index_chat(chat)

    assert calls and result.without_vectors == 0
    missing = await pool.fetchval("select count(*) from chunks where embedding is null")
    assert missing == 0
    assert await repo.get_threads_without_embeddings(chat_id) == []
