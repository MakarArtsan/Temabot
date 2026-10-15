"""Модерация лора и пересборка дайджеста (решение владельца, 15.10).

Лор — вики самых ярких местных мемов и историй: лишнее владелец убирает в
архив, продолжение истории сливается с ней в одну запись, а не плодит вторую.
Дайджест можно пересобрать и заменить им пост в группе — только по кнопке.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.db import repo
from src.db.models import Chat, Digest
from src.digest import lore as lore_mod
from src.digest import publish as pub
from src.digest.publish import PublishResult
from src.digest.render import DigestData, Topic
from src.llm.client import Usage
from src.web import app as web_app
from src.web import auth, membership

OWNER = 132036441
MEMBER = 555001
DAY = date(2026, 10, 14)


def chat(**kw: Any) -> Chat:
    defaults: dict[str, Any] = {"id": 1, "tg_id": -100111, "title": "Нейросети",
                                "collect": True, "digest": True, "publish": "manual"}
    return Chat(**{**defaults, **kw})


def lore_row(n: int, title: str, kind: str = "story", **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"id": n, "chat_id": 1, "kind": kind, "title": title,
                           "body": f"Описание «{title}».", "tg_user_id": None, "person": None,
                           "first_day": DAY, "last_day": DAY, "mentions": 1, "sources": [],
                           "hidden": False}
    return {**row, **kw}


class FakeLLM:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]], *, purpose: str, **kw: Any) -> Any:
        self.calls.append(messages)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, Usage(10, 5)


# ============================================================ лор: слияние моделью

@pytest.fixture
def lore_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"rows": {}, "merged": [], "added": [], "touched": []}

    async def list_lore(chat_id: int, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        rows = [r for r in state["rows"].values() if r["chat_id"] == chat_id]
        return rows if include_hidden else [r for r in rows if not r["hidden"]]

    async def get_lore(lore_id: int) -> dict[str, Any] | None:
        row = state["rows"].get(lore_id)
        return dict(row) if row else None

    async def merge_lore(into_id: int, from_id: int, *, title: str, body: str) -> Any:
        state["merged"].append((into_id, from_id, title, body))
        if title == "Занято":
            raise ValueError("запись с таким названием уже есть")
        state["rows"].pop(from_id, None)
        return {**state["rows"][into_id], "title": title, "body": body}

    async def add_lore(chat_id: int, **kw: Any) -> int:
        state["added"].append(kw)
        return 100 + len(state["added"])

    async def touch_lore(lore_id: int, chat_id: int, **kw: Any) -> bool:
        state["touched"].append(lore_id)
        return True

    async def opted_out_ids() -> set[int]:
        return set()

    for name, fn in {"list_lore": list_lore, "get_lore": get_lore, "merge_lore": merge_lore,
                     "add_lore": add_lore, "touch_lore": touch_lore,
                     "opted_out_ids": opted_out_ids}.items():
        monkeypatch.setattr(repo, name, fn)
    return state


def topics() -> list[Topic]:
    return [Topic(thread_id=41, title="Свадьба Миланы, серия вторая", kind="fun",
                  summary="Чат продолжил сватать Милану за Алмаза.", participants=["Милана"])]


async def test_daily_update_merges_a_continued_story(lore_db: dict[str, Any]):
    lore_db["rows"] = {
        1: lore_row(1, "Чат сватает Милану"),
        2: lore_row(2, "Свадьба Миланы: продолжение"),
        3: lore_row(3, "Свадьба Миланы, архивная", hidden=True),
        4: lore_row(4, "Мем про Милану", kind="meme"),
    }
    llm = FakeLLM({"add": [], "update": [], "merge": [
        {"into": 1, "from": 2, "body": "Сага о сватовстве: от мидий до клуба долбоночек."},
        {"into": 1, "from": 3},           # архив — решение владельца, не трогаем
        {"into": 1, "from": 4},           # разный вид — мем не история
        {"into": 1, "from": 1},
        {"into": 1, "from": 999},         # номер придуман
        {"into": "один", "from": 2},
        "не словарь",
    ]})

    await lore_mod.update_lore(chat(), DAY, topics(), llm=llm)

    assert lore_db["merged"] == [
        (1, 2, "Чат сватает Милану", "Сага о сватовстве: от мидий до клуба долбоночек.")]
    prompt = llm.calls[0][1]["content"]
    assert "3. [story] [архив] Свадьба Миланы, архивная" in prompt
    assert "Описание «Чат сватает Милану»." in prompt, "модель видит описание, чтобы дополнить"


async def test_daily_merges_are_capped_and_have_a_fallback_text(lore_db: dict[str, Any]):
    lore_db["rows"] = {i: lore_row(i, f"История {i}") for i in range(1, 10)}
    llm = FakeLLM({"merge": [{"into": 1, "from": i} for i in range(2, 10)]})

    await lore_mod.update_lore(chat(), DAY, topics(), llm=llm)

    assert len(lore_db["merged"]) == lore_mod.MAX_MERGES_PER_DAY
    assert lore_db["merged"][0][3] == "Описание «История 1». Описание «История 2».", \
        "без текста от модели описания ставятся подряд"


async def test_owner_merge_uses_the_model_and_survives_its_failure(lore_db: dict[str, Any]):
    lore_db["rows"] = {1: lore_row(1, "Палка Артёма"), 2: lore_row(2, "Артём и палка: финал"),
                       5: lore_row(5, "Чужая группа", chat_id=2)}
    llm = FakeLLM({"title": "Палка Артёма", "body": "Одна статья про палку, от начала до финала."})

    merged = await lore_mod.merge_entries(1, 2, llm=llm)

    assert merged is not None and merged["body"] == "Одна статья про палку, от начала до финала."
    assert "Артём и палка: финал" in llm.calls[0][1]["content"]

    lore_db["rows"][2] = lore_row(2, "Артём и палка: финал")
    await lore_mod.merge_entries(1, 2, llm=FakeLLM(RuntimeError("модель легла")))
    fallback = "Описание «Палка Артёма». Описание «Артём и палка: финал»."
    assert lore_db["merged"][-1][3] == fallback, "модель легла — описания подряд"

    assert await lore_mod.merge_entries(1, 5, llm=llm) is None, "только внутри одной группы"


# ============================================================ лор: страница владельца

@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # тот самый cfg, что у админки: test_config умеет перезагружать модуль настроек
    for conf in {id(auth.cfg): auth.cfg, id(web_app.cfg): web_app.cfg}.values():
        monkeypatch.setattr(conf, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
        monkeypatch.setattr(conf, "BOT_TOKEN", "123456:TEST-TOKEN")
        monkeypatch.setattr(conf, "OWNER_ID", OWNER)
        monkeypatch.setattr(conf, "BOT_USERNAME", "temabot")
        monkeypatch.setattr(conf, "WEB_BASE_URL", "https://admin.example.com")
        monkeypatch.setattr(conf, "DATABASE_URL", "")
    membership.forget()


def client_as(user_id: int) -> TestClient:
    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    client.cookies.set(auth.COOKIE_NAME, auth.issue_session(user_id))
    return client


def csrf_of(client: TestClient) -> str:
    return auth.read_session(client.cookies.get(auth.COOKIE_NAME))["csrf"]


@pytest.fixture
def lore_admin(lore_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    lore_db["rows"] = {
        1: lore_row(1, "Чат сватает Милану"),
        2: lore_row(2, "Свадьба Миланы: продолжение"),
        3: lore_row(3, "Скучная история", hidden=True),
        7: lore_row(7, "Чужая группа", chat_id=2),
    }
    lore_db["hidden"], lore_db["edited"], lore_db["deleted"] = [], [], []

    async def set_lore_hidden(lore_id: int, hidden: bool) -> dict[str, Any]:
        lore_db["hidden"].append((lore_id, hidden))
        lore_db["rows"][lore_id]["hidden"] = hidden
        return dict(lore_db["rows"][lore_id])

    async def edit_lore(lore_id: int, *, title: str, body: str) -> dict[str, Any]:
        lore_db["edited"].append((lore_id, title, body))
        if title == "Занято":
            raise ValueError("запись с таким названием уже есть")
        return {**lore_db["rows"][lore_id], "title": title, "body": body}

    async def delete_lore(lore_id: int) -> bool:
        lore_db["deleted"].append(lore_id)
        return True

    async def list_chats(**kw: Any) -> list[Chat]:
        return [chat()]

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return chat() if chat_id == 1 else None

    async def merge_entries(into_id: int, from_id: int, **kw: Any) -> Any:
        lore_db["merged"].append((into_id, from_id))
        return {"id": into_id}

    for name, fn in {"set_lore_hidden": set_lore_hidden, "edit_lore": edit_lore,
                     "delete_lore": delete_lore, "list_chats": list_chats,
                     "get_chat_by_id": get_chat_by_id}.items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(lore_mod, "merge_entries", merge_entries)
    return lore_db


def test_lore_page_keeps_the_archive_apart(lore_admin: dict[str, Any]):
    page = client_as(OWNER).get("/lore")

    assert page.status_code == 200
    archive = page.text.split("Скучная история", 1)
    assert len(archive) == 2, "архив виден владельцу"
    assert "Из архива" in page.text and "В архив" in page.text
    assert "Объединить с другой записью" in page.text
    # слить можно только с живыми записями этой группы
    options = page.text.split('name="into"', 1)[1].split("</select>", 1)[0]
    assert "Свадьба Миланы: продолжение" in options
    assert "Скучная история" not in options and "Чужая группа" not in options


def test_owner_archives_edits_and_deletes(lore_admin: dict[str, Any]):
    client = client_as(OWNER)
    csrf = csrf_of(client)

    hidden = client.post("/lore/1", data={"action": "hide", "csrf_token": csrf})
    assert hidden.status_code == 200 and "в архиве" in hidden.text and "Из архива" in hidden.text
    assert client.post("/lore/1", data={"action": "show", "csrf_token": csrf}).status_code == 200
    assert lore_admin["hidden"] == [(1, True), (1, False)]

    edited = client.post("/lore/2", data={"action": "edit", "csrf_token": csrf,
                                          "title": " Свадьба Миланы ", "body": "Финал.\nЭпилог."})
    assert edited.status_code == 200 and lore_admin["edited"] == [(2, "Свадьба Миланы",
                                                                   "Финал.\nЭпилог.")]
    assert "Эпилог." in edited.text
    assert client.post("/lore/2", data={"action": "edit", "csrf_token": csrf,
                                        "title": " "}).status_code == 400
    assert client.post("/lore/2", data={"action": "edit", "csrf_token": csrf,
                                        "title": "Занято"}).status_code == 400

    deleted = client.post("/lore/3", data={"action": "delete", "csrf_token": csrf})
    assert deleted.status_code == 200 and deleted.text == "" and lore_admin["deleted"] == [3]
    assert client.post("/lore/1", data={"action": "взлом", "csrf_token": csrf}).status_code == 400
    assert client.post("/lore/99", data={"action": "hide", "csrf_token": csrf}).status_code == 404


def test_owner_merges_within_one_group(lore_admin: dict[str, Any]):
    client = client_as(OWNER)
    csrf = csrf_of(client)

    merged = client.post("/lore/2", data={"action": "merge", "into": "1", "csrf_token": csrf})
    assert merged.status_code == 204 and merged.headers["HX-Redirect"] == "/lore?chat=1"
    assert lore_admin["merged"] == [(1, 2)], "вливаемая — та, на которой нажали"

    for into in ("7", "2", "99"):   # чужая группа, сама в себя, нет такой
        response = client.post("/lore/2", data={"action": "merge", "into": into,
                                                "csrf_token": csrf})
        assert response.status_code == 400, into
    assert lore_admin["merged"] == [(1, 2)]


def test_lore_moderation_is_owner_only(lore_admin: dict[str, Any]):
    client = client_as(MEMBER)
    for data in ({"action": "hide"}, {"action": "delete"}, {"action": "merge", "into": "1"}):
        response = client.post("/lore/2", data={**data, "csrf_token": csrf_of(client)})
        assert response.status_code == 303 and response.headers["location"] == "/login"
    assert client.post("/lore/1", data={"action": "hide", "csrf_token": "мимо"}).status_code == 303
    owner = client_as(OWNER)
    assert owner.post("/lore/1", data={"action": "hide", "csrf_token": "мимо"}).status_code == 403
    assert lore_admin["hidden"] == [] and lore_admin["deleted"] == [] and lore_admin["merged"] == []


# ============================================================ пересборка и замена поста

def digest(published: bool = False) -> Digest:
    from datetime import UTC, datetime

    data = DigestData(
        chat_tg_id=-100111, day=DAY, chat_title="Нейросети",
        topics=[Topic(thread_id=11, title="Veo подешевел", takeaway="12 ₽ за ролик", msg_count=9)],
        life={"headline": "Расследование: кто сосватал Милану",
              "lead": "Редакции стало известно…",
              "episodes": [{"title": "Клубное чадо", "text": "Имя ищут всем чатом.",
                            "thread_ids": [41]}]},
        msg_count=120, participants=14,
    )
    return Digest(id=5, chat_id=1, day=DAY, summary_md="", payload=data.to_dict(),
                  published_at=datetime(2026, 10, 14, 18, 0, tzinfo=UTC) if published else None,
                  published_msg_ids=[501] if published else [])


@pytest.fixture
def digest_admin(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"published": False, "launched": [], "republished": []}

    async def get_digest_by_id(digest_id: int) -> Digest | None:
        return digest(state["published"]) if digest_id == 5 else None

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return chat() if chat_id == 1 else None

    async def get_digest_items(digest_id: int) -> list[dict[str, Any]]:
        return [
            {"id": 1, "thread_id": 11, "title": "Veo подешевел", "shown": True, "score": 8.0,
             "kind": "insight", "takeaway": "12 ₽", "features": {}, "feedback": None,
             "msg_count": 9},
            {"id": 2, "thread_id": 41, "title": "Клубное чадо", "shown": False, "score": 2.0,
             "kind": "fun", "takeaway": "", "features": {}, "feedback": None, "msg_count": 30},
        ]

    def launch_digest(chat: Chat, day: date, **kw: Any) -> Any:
        state["launched"].append((chat.id, day))
        return SimpleNamespace(id="job-1", done=False, resumed=0, elapsed=0, title="дайджест",
                               error=None, result=None)

    async def republish(bot: Any, digest_id: int) -> PublishResult:
        state["republished"].append(digest_id)
        return PublishResult(True, "Заменил пост в группе", message_ids=[777])

    class FakeBot:
        class session:   # как у aiogram.Bot: bot.session.close()
            @staticmethod
            async def close() -> None:
                return None

    for name, fn in {"get_digest_by_id": get_digest_by_id, "get_chat_by_id": get_chat_by_id,
                     "get_digest_items": get_digest_items}.items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(web_app, "launch_digest", launch_digest)
    monkeypatch.setattr(web_app, "republish_digest", republish)
    monkeypatch.setattr(web_app, "make_bot", FakeBot)
    return state


def test_digest_page_has_the_life_column_and_a_rebuild_button(digest_admin: dict[str, Any]):
    page = client_as(OWNER).get("/digests/5")

    assert page.status_code == 200
    assert "Расследование: кто сосватал Милану" in page.text
    assert "Из чего собрана колонка" in page.text, "видно, какие разговоры ушли в колонку"
    assert 'hx-post="/digests/5/rebuild"' in page.text
    assert "Заменить пост в группе" not in page.text, "ещё не опубликован — нечего заменять"


def test_rebuild_runs_in_the_background_and_publishes_nothing(digest_admin: dict[str, Any],
                                                              monkeypatch: pytest.MonkeyPatch):
    async def explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("пересборка сама в группу не публикует")

    monkeypatch.setattr(web_app, "publish_digest", explode)
    client = client_as(OWNER)

    response = client.post("/digests/5/rebuild", data={"csrf_token": csrf_of(client)})

    assert response.status_code == 200 and "Собираю" in response.text
    assert digest_admin["launched"] == [(1, DAY)]
    assert client.post("/digests/9/rebuild",
                       data={"csrf_token": csrf_of(client)}).status_code == 404


def test_published_digest_can_be_replaced(digest_admin: dict[str, Any]):
    digest_admin["published"] = True
    client = client_as(OWNER)

    page = client.get("/digests/5")
    assert "Заменить пост в группе" in page.text and 'hx-post="/digests/5/republish"' in page.text

    response = client.post("/digests/5/republish", data={"csrf_token": csrf_of(client)})
    assert response.status_code == 200 and "Заменил пост" in response.text
    assert digest_admin["republished"] == [5]


def test_rebuild_and_replace_are_owner_only(digest_admin: dict[str, Any]):
    member = client_as(MEMBER)
    for path in ("/digests/5/rebuild", "/digests/5/republish"):
        response = member.post(path, data={"csrf_token": csrf_of(member)})
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert client_as(OWNER).post(path, data={"csrf_token": "мимо"}).status_code == 403
    assert digest_admin["launched"] == [] and digest_admin["republished"] == []


async def test_replace_removes_the_old_post_first(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    async def unpublish(bot: Any, digest_id: int) -> PublishResult:
        calls.append("unpublish")
        return PublishResult(True, "Убрал")

    async def publish(bot: Any, digest_id: int) -> PublishResult:
        calls.append("publish")
        return PublishResult(True, "Опубликовал", message_ids=[777])

    monkeypatch.setattr(pub, "unpublish_digest", unpublish)
    monkeypatch.setattr(pub, "publish_digest", publish)

    result = await pub.republish_digest(object(), 5)
    assert result.ok and calls == ["unpublish", "publish"]


async def test_replace_does_not_double_post_when_the_old_one_stays(
    monkeypatch: pytest.MonkeyPatch,
):
    """Бот удаляет свои сообщения только 48 часов — тогда второй выпуск не выкладываем."""
    async def unpublish(bot: Any, digest_id: int) -> PublishResult:
        return PublishResult(False, "Telegram не дал удалить старый пост")

    async def publish(bot: Any, digest_id: int) -> PublishResult:
        raise AssertionError("в группе оказались бы два выпуска за день")

    monkeypatch.setattr(pub, "unpublish_digest", unpublish)
    monkeypatch.setattr(pub, "publish_digest", publish)

    result = await pub.republish_digest(object(), 5)
    assert not result.ok and "Новый пост не публиковал" in result.text


# ============================================================ настоящая база

@pytest.mark.usefixtures("db")
async def test_merge_in_the_real_database():
    stored = await repo.bootstrap_primary_chat(-100111)
    other = await repo.bootstrap_primary_chat(-100222)
    saga = await repo.add_lore(stored.id, kind="story", title="Сватовство Миланы", body="Начало.",
                               day=date(2026, 10, 10),
                               sources=[{"day": "2026-10-10", "msg_id": 1}])
    sequel = await repo.add_lore(stored.id, kind="story", title="Свадьба Миланы", body="Финал.",
                                 day=DAY, sources=[{"day": DAY.isoformat(), "msg_id": 2}])
    meme = await repo.add_lore(stored.id, kind="meme", title="Клуб долбоночек", body="",
                               day=DAY)
    alien = await repo.add_lore(other.id, kind="story", title="Чужая", body="", day=DAY)
    assert saga and sequel and meme and alien
    await repo.touch_lore(sequel, stored.id, day=date(2026, 10, 15))

    assert await repo.merge_lore(saga, alien, title="x", body="y") is None, "чужая группа"
    assert await repo.merge_lore(saga, saga, title="x", body="y") is None

    taken = await repo.add_lore(stored.id, kind="story", title="Ещё история", body="", day=DAY)
    assert taken
    with pytest.raises(ValueError):
        await repo.merge_lore(saga, sequel, title="ЕЩЁ история", body="x")
    assert await repo.get_lore(sequel) is not None, "сбой слияния откатывается целиком"

    merged = await repo.merge_lore(saga, sequel, title="Сватовство Миланы",
                                   body="Начало. Финал.")
    assert merged is not None
    assert merged["body"] == "Начало. Финал." and merged["mentions"] == 3
    assert merged["first_day"] == date(2026, 10, 10) and merged["last_day"] == date(2026, 10, 15)
    assert [s["msg_id"] for s in merged["sources"]] == [1, 2]
    assert await repo.get_lore(sequel) is None, "вливаемая запись удалена"

    edited = await repo.edit_lore(saga, title="Сага о Милане", body="Одной статьёй.")
    assert edited is not None and edited["title"] == "Сага о Милане"
    with pytest.raises(ValueError):
        await repo.edit_lore(saga, title="ещё ИСТОРИЯ", body="")
    assert (await repo.get_lore(saga) or {})["title"] == "Сага о Милане"
