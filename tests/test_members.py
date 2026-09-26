"""Названия групп и список участников (для страницы участников, TZ §9)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from src.collector import members as members_mod
from src.collector.service import Collector
from src.db import repo
from src.db.models import Chat
from src.digest.render import DigestData

GROUP = -1002354231333


def _chat(**kw: Any) -> Chat:
    return Chat(id=7, tg_id=GROUP, collect=True, **kw)


# ================================================================= названия

def test_stored_digest_gets_the_current_title():
    """Дайджест, собранный до того, как узнали название, показывал номер группы."""
    data = DigestData(chat_tg_id=GROUP, day=date(2026, 9, 25), chat_title="")

    assert data.titled("Нейросети и видео").chat_title == "Нейросети и видео"
    assert data.titled(None) is data, "без названия ничего не меняем"
    assert data.titled("") is data


class TitleClient:
    def __init__(self, titles: dict[int, Any]) -> None:
        self.titles = titles

    async def get_entity(self, tg_id: int) -> Any:
        title = self.titles[tg_id]
        if isinstance(title, Exception):
            raise title
        return SimpleNamespace(title=title)


async def test_collector_saves_real_titles(monkeypatch: pytest.MonkeyPatch):
    saved: list[tuple[int, Any]] = []

    async def set_chat_title(tg_id: int, title: Any) -> bool:
        saved.append((tg_id, title))
        return True

    monkeypatch.setattr(repo, "set_chat_title", set_chat_title)
    client = TitleClient({GROUP: "Нейросети и видео", -100222: ConnectionError("нет сети")})
    chats = [_chat(), Chat(id=8, tg_id=-100222, collect=True)]

    updated = await Collector(client).refresh_titles(chats)

    assert updated == 1, "ошибка на одной группе не мешает остальным"
    assert saved == [(GROUP, "Нейросети и видео")]


async def test_dry_run_collector_does_not_write_titles(monkeypatch: pytest.MonkeyPatch):
    async def explode(*a: Any, **kw: Any) -> bool:
        raise AssertionError("dry-run не пишет в БД")

    monkeypatch.setattr(repo, "set_chat_title", explode)
    assert await Collector(TitleClient({}), dry_run=True).refresh_titles([_chat()]) == 0


async def test_renamed_group_and_members_are_tracked(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, Any]] = []

    async def set_chat_title(tg_id: int, title: str) -> bool:
        calls.append(("title", title))
        return True

    async def add_chat_member(chat_id: int, uid: int) -> None:
        calls.append(("add", uid))

    async def remove_chat_member(chat_id: int, uid: int) -> None:
        calls.append(("remove", uid))

    monkeypatch.setattr(repo, "set_chat_title", set_chat_title)
    monkeypatch.setattr(repo, "add_chat_member", add_chat_member)
    monkeypatch.setattr(repo, "remove_chat_member", remove_chat_member)

    collector = Collector(TitleClient({}))
    collector._chats_by_tg_id = {GROUP: _chat()}

    def event(**kw: Any) -> Any:
        base = dict(chat_id=GROUP, new_title=None, user_ids=[], user_joined=False,
                    user_added=False, user_left=False, user_kicked=False)
        base.update(kw)
        return SimpleNamespace(**base)

    await collector.on_chat_action(event(new_title="Новое имя"))
    await collector.on_chat_action(event(user_joined=True, user_ids=[11]))
    await collector.on_chat_action(event(user_added=True, user_ids=[12, 13]))
    await collector.on_chat_action(event(user_left=True, user_ids=[11]))
    await collector.on_chat_action(event(user_kicked=True, user_ids=[12]))
    await collector.on_chat_action(event(chat_id=-100999, user_joined=True, user_ids=[99]))

    assert calls == [
        ("title", "Новое имя"),
        ("add", 11), ("add", 12), ("add", 13),
        ("remove", 11), ("remove", 12),
    ], "события чужих групп не учитываются"


async def test_bot_refreshes_titles_where_it_is_a_member(monkeypatch: pytest.MonkeyPatch):
    from src.bot import handlers_admin

    saved: list[tuple[int, Any]] = []

    async def list_chats(**kw: Any) -> list[Chat]:
        return [_chat(), Chat(id=8, tg_id=-100222)]

    async def set_chat_title(tg_id: int, title: Any) -> bool:
        saved.append((tg_id, title))
        return True

    class FakeBot:
        async def get_chat(self, tg_id: int) -> Any:
            if tg_id == -100222:
                raise RuntimeError("chat not found")
            return SimpleNamespace(title="Нейросети и видео")

        async def send_message(self, *a: Any, **kw: Any) -> None:
            raise AssertionError("обновление названий ничего не отправляет")

    monkeypatch.setattr(handlers_admin.repo, "list_chats", list_chats)
    monkeypatch.setattr(handlers_admin.repo, "set_chat_title", set_chat_title)

    assert await handlers_admin.refresh_chat_titles(FakeBot()) == 1
    assert saved == [(GROUP, "Нейросети и видео")]


def test_titles_are_refreshed_nightly():
    from src.digest import scheduler as sched

    job = sched.build_scheduler(bot=object()).get_job("chat_titles")
    assert job is not None


# ============================================================ сверка участников

class Participants(list):
    """Как TotalList у Telethon: список плюс общее число."""

    def __init__(self, items: list[Any], total: int) -> None:
        super().__init__(items)
        self.total = total


def _user(uid: int, **kw: Any) -> Any:
    return SimpleNamespace(id=uid, bot=kw.get("bot", False), deleted=kw.get("deleted", False))


class MembersClient:
    def __init__(self, users: Any, *, hidden: bool = False, admin: bool = False) -> None:
        self.users = users
        self.hidden = hidden
        self.admin = admin
        self.asked = 0

    async def get_entity(self, tg_id: int) -> Any:
        rights = object() if self.admin else None
        return SimpleNamespace(id=tg_id, creator=False, admin_rights=rights)

    async def __call__(self, request: Any) -> Any:
        return SimpleNamespace(full_chat=SimpleNamespace(participants_hidden=self.hidden))

    async def get_participants(self, entity: Any) -> Any:
        self.asked += 1
        if isinstance(self.users, Exception):
            raise self.users
        return self.users


@pytest.fixture
def saved_members(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[int], bool]]:
    saved: list[tuple[list[int], bool]] = []

    async def save_chat_members(chat_id: int, ids: list[int], *, complete: bool) -> None:
        saved.append((ids, complete))

    monkeypatch.setattr(repo, "save_chat_members", save_chat_members)
    return saved


async def test_full_member_list_is_saved_without_bots(saved_members: list[Any]):
    users = Participants([_user(1), _user(2), _user(3, bot=True), _user(4, deleted=True)], 4)

    assert await members_mod.sync_members(MembersClient(users), _chat()) == 2
    assert saved_members == [([1, 2], True)]


async def test_partial_list_only_adds(saved_members: list[Any]):
    """Большая группа: Telegram отдал не всех — выкидывать остальных нельзя."""
    users = Participants([_user(1), _user(2)], 15_000)

    await members_mod.sync_members(MembersClient(users), _chat())
    assert saved_members == [([1, 2], False)]


async def test_hidden_member_list_is_not_trusted(saved_members: list[Any]):
    """Скрытый список обычному участнику покажет одних админов."""
    client = MembersClient(Participants([_user(1)], 1), hidden=True)

    assert await members_mod.sync_members(client, _chat()) is None
    assert client.asked == 0 and saved_members == []


async def test_hidden_list_is_fine_for_an_admin(saved_members: list[Any]):
    client = MembersClient(Participants([_user(1), _user(2)], 2), hidden=True, admin=True)

    assert await members_mod.sync_members(client, _chat()) == 2


async def test_empty_or_failed_answer_keeps_the_old_list(saved_members: list[Any]):
    assert await members_mod.sync_members(MembersClient(Participants([], 0)), _chat()) is None
    assert await members_mod.sync_members(MembersClient(RuntimeError("сбой")), _chat()) is None
    assert saved_members == []


# ======================================================== в настоящей базе

@pytest.mark.usefixtures("db")
async def test_set_chat_title_changes_only_real_names():
    chat = await repo.bootstrap_primary_chat(GROUP)
    assert chat.title is None

    assert await repo.set_chat_title(GROUP, "  Нейросети и видео ") is True
    assert await repo.set_chat_title(GROUP, "Нейросети и видео") is False, "то же самое"
    assert await repo.set_chat_title(GROUP, "") is False, "пустое не затирает"
    assert await repo.set_chat_title(GROUP, None) is False

    stored = await repo.get_chat_by_tg_id(GROUP)
    assert stored is not None and stored.title == "Нейросети и видео"


@pytest.mark.usefixtures("db")
async def test_member_list_lifecycle():
    chat = await repo.bootstrap_primary_chat(GROUP)

    assert await repo.chat_member_status(chat.id, 1) is None, "списка ещё нет — не знаем"

    await repo.save_chat_members(chat.id, [1, 2, 3], complete=True)
    assert await repo.chat_member_status(chat.id, 1) is True
    assert await repo.chat_member_status(chat.id, 99) is False

    # полный список: кто пропал из него, тот вышел
    await repo.save_chat_members(chat.id, [2, 3], complete=True)
    assert await repo.chat_member_status(chat.id, 1) is False

    # неполный — только добавляет
    await repo.save_chat_members(chat.id, [4], complete=False)
    assert await repo.chat_member_status(chat.id, 2) is True
    assert await repo.chat_member_status(chat.id, 4) is True

    await repo.remove_chat_member(chat.id, 2)
    await repo.add_chat_member(chat.id, 5)
    assert await repo.chat_member_status(chat.id, 2) is False
    assert await repo.chat_member_status(chat.id, 5) is True


@pytest.mark.usefixtures("db")
async def test_stale_member_list_is_not_trusted():
    chat = await repo.bootstrap_primary_chat(GROUP)
    await repo.save_chat_members(chat.id, [1], complete=True)

    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await repo.set_state(f"members:{chat.id}", {"at": old, "count": 1, "complete": True})

    assert await repo.chat_member_status(chat.id, 1) is None


@pytest.mark.usefixtures("db")
async def test_portal_mode_is_stored_and_checked():
    import asyncpg

    await repo.bootstrap_primary_chat(GROUP)
    assert (await repo.get_chat_by_tg_id(GROUP)).portal == "off", "по умолчанию закрыта"
    assert await repo.list_portal_chats() == []

    await repo.set_chat_flags(GROUP, portal="digests")
    assert [c.tg_id for c in await repo.list_portal_chats()] == [GROUP]

    with pytest.raises(asyncpg.CheckViolationError):
        await repo.set_chat_flags(GROUP, portal="всем")


@pytest.mark.usefixtures("db")
async def test_chat_digests_archive_is_newest_first():
    chat = await repo.bootstrap_primary_chat(GROUP)
    for day in (date(2026, 9, 23), date(2026, 9, 25), date(2026, 9, 24)):
        await repo.save_digest(chat.id, day, "текст", {"topics": []}, msg_count=5)

    days = [d.day for d in await repo.list_chat_digests(chat.id)]
    assert days == [date(2026, 9, 25), date(2026, 9, 24), date(2026, 9, 23)]
