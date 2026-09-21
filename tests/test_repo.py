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
        chat_id, day, "вторая версия", topics=[{"title": "Seedance"}], msg_count=12
    )

    assert first == second
    stored = await repo.get_digest(chat_id, day)
    assert stored is not None
    assert stored.summary_md == "вторая версия"
    assert stored.topics == [{"title": "Seedance"}]
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
