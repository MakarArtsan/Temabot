"""Досье участников: вехи и характеристика (решение владельца, 30.10).

Характеристика — по-доброму и с юмором; здоровье, деньги, ссоры не записываются.
По умолчанию досье видит только владелец; участникам — когда он откроет галочкой,
и без тех, кто скрылся по /optout.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.db import repo
from src.db.models import Chat, Digest
from src.digest import dossier as dossier_mod
from src.digest import prompts
from src.digest.render import DigestData, Topic
from src.llm.client import Usage
from src.web import app as web_app
from src.web import auth, membership, portal

OWNER = 132036441
MEMBER = 555001
SASHA, POLINA, DARIA, HIDDEN_GUY = 201, 202, 203, 204
DAY = date(2026, 10, 29)
PEOPLE = {SASHA: "Sasha Fevralskaya", POLINA: "Полина Кошкина", DARIA: "Дария byEDINOROG.PRO",
          HIDDEN_GUY: "Скрытный Вася"}


def chat(**kw: Any) -> Chat:
    return Chat(**{"id": 1, "tg_id": -1002354231333, "title": "Группа", "portal": "all", **kw})


TOPICS = [
    Topic(thread_id=341412, title="Саша Февральская взяла первое место и $7000",
          summary="Sasha Fevralskaya выиграла международный конкурс хорроров, чат поздравлял",
          participants=["Sasha Fevralskaya", "Полина Кошкина"], key_msg_ids=[341551]),
    Topic(thread_id=341071, title="Полина Кошкина продала фильм «Бюро проклятий»",
          summary="У Полины выкупили фильм, детали под NDA", participants=["Полина Кошкина"]),
    Topic(thread_id=340000, title="Пустой тред", participants=["Дария"]),
]


class DossierLLM:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, messages: list[dict[str, str]], *, purpose: str, **kw: Any) -> Any:
        self.calls.append({"purpose": purpose, "system": messages[0]["content"],
                           "user": messages[1]["content"]})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, Usage(100, 50)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"existing": [], "updated": [], "opted_out": set()}

    async def list_dossiers(chat_id: int, **kw: Any) -> list[dict[str, Any]]:
        return list(state["existing"])

    async def update_dossier(chat_id: int, uid: int, **kw: Any) -> bool:
        state["updated"].append((uid, kw))
        return True

    async def opted_out_ids() -> set[int]:
        return state["opted_out"]

    monkeypatch.setattr(repo, "list_dossiers", list_dossiers)
    monkeypatch.setattr(repo, "update_dossier", update_dossier)
    monkeypatch.setattr(repo, "opted_out_ids", opted_out_ids)
    return state


# ============================================================ кто попадает в промпт

def test_cast_is_people_mentioned_in_the_topics():
    cast = dossier_mod.day_cast(TOPICS[:2], PEOPLE)
    assert set(cast) == {SASHA, POLINA}, "Дария в этих темах не участвовала"
    # «Дария byEDINOROG.PRO» находится по первому слову имени
    dasha = [Topic(thread_id=1, title="Дария посчитала SeeD", summary="8460 токенов")]
    assert dossier_mod.day_cast(dasha, PEOPLE) == {DARIA: PEOPLE[DARIA]}


async def test_prompt_has_people_what_is_known_and_the_rules(store: dict[str, Any]):
    store["existing"] = [{"tg_user_id": SASHA, "summary": "Мастер мрачных мультов",
                          "milestones": [{"text": "финалист конкурса Refuge"}], "hidden": False}]
    llm = DossierLLM({"people": []})

    await dossier_mod.update_dossiers(chat(), DAY, TOPICS, PEOPLE, llm=llm)

    call = llm.calls[0]
    assert call["purpose"] == "dossier" and "29.10.2026" in call["system"]
    assert "здоровье" in prompts.DOSSIER_SYSTEM and "по-доброму" in prompts.DOSSIER_SYSTEM
    assert f"{SASHA} — Sasha Fevralskaya" in call["user"]
    assert "характеристика: Мастер мрачных мультов" in call["user"]
    assert "вехи: финалист конкурса Refuge" in call["user"]
    assert "[сообщения: 341412, 341551]" in call["user"]
    assert "Пустой тред" not in call["user"], "без пересказа — не материал"


async def test_answer_is_checked(store: dict[str, Any]):
    store["existing"] = [{"tg_user_id": HIDDEN_GUY, "hidden": True, "milestones": []}]
    llm = DossierLLM({"people": [
        {"who": SASHA, "summary": "  Королева хоррора: берёт первые места и не зазнаётся. ",
         "milestones": [{"text": "1 место в AI Horror Film Competition, $7000",
                         "msg_ids": [999, "341551"]},
                        "строкой тоже можно", {"text": ""}, {"text": "четвёртая — лишняя"}]},
        {"who": POLINA, "milestones": [], "summary": ""},           # нечего записать
        {"who": 777, "summary": "чужой человек"},                   # не из списка
        {"who": "Саша", "summary": "не номер"},
    ]})

    touched = await dossier_mod.update_dossiers(chat(), DAY, TOPICS, PEOPLE, llm=llm)

    assert touched == ["Sasha Fevralskaya"]
    uid, saved = store["updated"][0]
    assert uid == SASHA and saved["day"] == DAY
    assert saved["summary"] == "Королева хоррора: берёт первые места и не зазнаётся."
    assert saved["milestones"] == [
        {"day": "2026-10-29", "text": "1 место в AI Horror Film Competition, $7000",
         "msg_id": 341551},
        {"day": "2026-10-29", "text": "строкой тоже можно", "msg_id": None},
    ], "только настоящие номера сообщений, не больше трёх вех, пустые — мимо"


async def test_opted_out_and_hidden_people_are_left_alone(store: dict[str, Any]):
    store["opted_out"] = {SASHA}
    store["existing"] = [{"tg_user_id": POLINA, "hidden": True, "milestones": []}]
    llm = DossierLLM({"people": [{"who": SASHA, "summary": "x"},
                                 {"who": POLINA, "summary": "y"}]})

    assert await dossier_mod.update_dossiers(chat(), DAY, TOPICS, PEOPLE, llm=llm) == []
    assert llm.calls == [], "кроме них в темах никого — модель не спрашиваем"
    assert store["updated"] == []


async def test_switched_off_or_nothing_to_tell(store: dict[str, Any]):
    llm = DossierLLM({"people": [{"who": SASHA, "summary": "x"}]})
    off = chat(settings={"dossier": {"enabled": False}})
    assert await dossier_mod.update_dossiers(off, DAY, TOPICS, PEOPLE, llm=llm) == []
    assert await dossier_mod.update_dossiers(chat(), DAY, [TOPICS[2]], PEOPLE, llm=llm) == []
    assert await dossier_mod.update_dossiers(chat(), DAY, TOPICS, {}, llm=llm) == []
    assert llm.calls == []


def test_private_by_default():
    assert dossier_mod.enabled(chat()) is True
    assert dossier_mod.public(chat()) is False
    assert dossier_mod.public(chat(settings={"dossier": {"public": True}})) is True


async def test_digest_survives_a_dossier_failure(store: dict[str, Any]):
    from src.digest import pipeline as dp

    result = dp.DigestResult(chat_tg_id=-1, day=DAY, markdown="", topics=[], usage=Usage(0, 0),
                             msg_count=0, all_topics=TOPICS, people=PEOPLE)
    llm = DossierLLM(RuntimeError("модель легла"))
    assert await dp._update_dossiers(chat(), DAY, result, llm=llm) == []


async def test_backfill_walks_the_archive(store: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    def digest(n: int, day: date) -> Digest:
        data = DigestData(chat_tg_id=-1, day=day, topics=[TOPICS[0]])
        return Digest(id=n, chat_id=1, day=day, summary_md="", payload=data.to_dict())

    async def list_chat_digests(chat_id: int, limit: int = 60) -> list[Digest]:
        return [digest(2, DAY), digest(1, date(2026, 9, 25)), digest(0, date(2026, 6, 1))]

    async def get_digest_items(digest_id: int, **kw: Any) -> list[dict[str, Any]]:
        if digest_id == 1:
            raise RuntimeError("база моргнула")
        return [{"thread_id": 341412, "title": "Победа", "kind": "announcement",
                 "features": {}}]

    seen_cuts: list[Any] = []

    async def day_people(chat_id: int, day: date, cut: Any = None) -> dict[int, str]:
        seen_cuts.append(cut)
        return PEOPLE

    monkeypatch.setattr(repo, "list_chat_digests", list_chat_digests)
    monkeypatch.setattr(repo, "get_digest_items", get_digest_items)
    monkeypatch.setattr(repo, "day_people", day_people)
    notes: list[str] = []
    llm = DossierLLM({"people": [{"who": SASHA, "summary": "Королева хоррора"}]})

    touched = await dossier_mod.backfill(chat(), date(2026, 10, 30), llm=llm,
                                         progress=notes.append)

    assert notes == ["1 из 2: 25.09", "2 из 2: 29.10"], "от старых к новым, старше — мимо"
    assert touched == ["Sasha Fevralskaya"] and len(llm.calls) == 1


# ============================================================ база

@pytest.mark.usefixtures("db")
async def test_dossier_in_the_real_database():
    stored = await repo.bootstrap_primary_chat(-1002354231333)
    await repo.upsert_author(SASHA, "Sasha Fevralskaya", username="elheor")
    await repo.upsert_author(POLINA, "Полина")
    await repo.upsert_author(HIDDEN_GUY, "Вася")
    await repo.set_author_flags(HIDDEN_GUY, hide_from_ratings=True)

    first = [{"day": "2026-10-08", "text": "Финалист Refuge", "msg_id": 1}]
    assert await repo.update_dossier(stored.id, SASHA, day=date(2026, 10, 8), milestones=first,
                                     summary="Мастер мрачных мультов")
    await repo.update_dossier(stored.id, SASHA, day=DAY, milestones=[
        {"day": "2026-10-29", "text": "финалист refuge", "msg_id": 2},     # повтор
        {"day": "2026-10-29", "text": "1 место и $7000", "msg_id": 3},
    ])
    await repo.update_dossier(stored.id, POLINA, day=DAY, milestones=[
        {"day": "2026-10-29", "text": "продала фильм", "msg_id": None}])    # без характеристики
    await repo.update_dossier(stored.id, HIDDEN_GUY, day=DAY, summary="Тихий гений")

    sasha = await repo.get_dossier(stored.id, SASHA)
    assert sasha is not None
    assert [m["text"] for m in sasha["milestones"]] == ["Финалист Refuge", "1 место и $7000"]
    assert sasha["summary"] == "Мастер мрачных мультов", "без новой — старая остаётся"
    assert (sasha["first_day"], sasha["last_day"]) == (date(2026, 10, 8), DAY)
    assert sasha["name"] == "Sasha Fevralskaya" and sasha["username"] == "elheor"

    public = [r["tg_user_id"] for r in await repo.list_dossiers(stored.id, public=True)]
    assert public == [SASHA], "без пустых характеристик и без скрывшихся по /optout"
    assert len(await repo.list_dossiers(stored.id)) == 3

    # владелец правит: веха, скрыть, характеристика, удалить
    assert await repo.delete_milestone(stored.id, SASHA, 0)
    assert not await repo.delete_milestone(stored.id, SASHA, 5)
    assert [m["text"] for m in (await repo.get_dossier(stored.id, SASHA) or {})["milestones"]] \
        == ["1 место и $7000"]
    await repo.set_dossier_hidden(stored.id, SASHA, True)
    assert not await repo.update_dossier(stored.id, SASHA, day=DAY, summary="перезапись")
    assert (await repo.get_dossier(stored.id, SASHA) or {})["summary"] == "Мастер мрачных мультов"
    assert await repo.list_dossiers(stored.id, public=True) == []
    await repo.edit_dossier_summary(stored.id, SASHA, "Королева хоррора")
    assert (await repo.get_dossier(stored.id, SASHA) or {})["summary"] == "Королева хоррора"
    assert await repo.delete_dossier(stored.id, POLINA)
    assert await repo.get_dossier(stored.id, POLINA) is None


@pytest.mark.usefixtures("db")
async def test_milestones_are_capped(monkeypatch: pytest.MonkeyPatch):
    stored = await repo.bootstrap_primary_chat(-1002354231333)
    monkeypatch.setattr(repo, "DOSSIER_MILESTONES_MAX", 3)
    for n in range(5):
        await repo.update_dossier(stored.id, SASHA, day=DAY,
                                  milestones=[{"day": "2026-10-29", "text": f"веха {n}"}])
    row = await repo.get_dossier(stored.id, SASHA)
    assert row is not None
    assert [m["text"] for m in row["milestones"]] == ["веха 2", "веха 3", "веха 4"]


# ============================================================ админка и страница участников

@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for conf in {id(auth.cfg): auth.cfg, id(web_app.cfg): web_app.cfg,
                 id(portal.cfg): portal.cfg}.values():
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


ROWS = [
    {"chat_id": 1, "tg_user_id": SASHA, "name": "Sasha <b>Fevralskaya</b>", "username": "elheor",
     "summary": "Королева хоррора", "hidden": False, "opted_out": False,
     "milestones": [{"day": "2026-10-08", "text": "Финалист Refuge", "msg_id": 1},
                    {"day": "2026-10-29", "text": "1 место и $7000", "msg_id": 341551}]},
    {"chat_id": 1, "tg_user_id": POLINA, "name": "Полина", "username": None, "summary": "",
     "hidden": True, "opted_out": False, "milestones": []},
]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"settings": {}, "portal": "all", "calls": [], "public_asked": []}

    def current() -> Chat:
        return chat(portal=state["portal"], settings={"dossier": dict(state["settings"])})

    async def list_chats(**kw: Any) -> list[Chat]:
        return [current()]

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return current() if chat_id == 1 else None

    async def list_dossiers(chat_id: int, *, include_hidden: bool = False,
                            public: bool = False) -> list[dict[str, Any]]:
        state["public_asked"].append(public)
        rows = [r for r in ROWS if include_hidden or not r["hidden"]]
        return [dict(r) for r in rows if not public or r["summary"]]

    async def get_dossier(chat_id: int, uid: int) -> dict[str, Any] | None:
        return next((dict(r) for r in ROWS if r["tg_user_id"] == uid), None)

    async def update_chat_settings(chat_id: int, patch: dict[str, Any]) -> None:
        state["settings"] = patch["dossier"]

    def recorder(name: str) -> Any:
        async def call(*args: Any, **kw: Any) -> bool:
            state["calls"].append((name, args))
            return name != "delete_milestone" or args[2] < 2
        return call

    async def is_member(chat: Chat, user_id: int, **kw: Any) -> bool:
        return user_id in (OWNER, MEMBER)

    for name, fn in {"list_chats": list_chats, "get_chat_by_id": get_chat_by_id,
                     "list_dossiers": list_dossiers, "get_dossier": get_dossier,
                     "update_chat_settings": update_chat_settings}.items():
        monkeypatch.setattr(repo, name, fn)
    for name in ("edit_dossier_summary", "delete_milestone", "set_dossier_hidden",
                 "delete_dossier"):
        monkeypatch.setattr(repo, name, recorder(name))
    monkeypatch.setattr(membership, "is_member", is_member)
    return state


def test_owner_sees_and_edits_dossiers(world: dict[str, Any]):
    client = client_as(OWNER)
    page = client.get("/dossiers")

    assert page.status_code == 200
    assert "Sasha &lt;b&gt;Fevralskaya&lt;/b&gt;" in page.text and "<b>Fevralskaya" not in page.text
    assert "Королева хоррора" in page.text and "1 место и $7000" in page.text
    assert "Скрытые" in page.text and "Показывать участникам" in page.text
    assert 'aria-label="Показывать участникам" aria-checked="false"' in page.text

    csrf = csrf_of(client)
    url = f"/dossiers/1/{SASHA}"
    assert client.post(url, data={"action": "summary", "summary": " Новая ",
                                  "csrf_token": csrf}).status_code == 200
    assert client.post(url, data={"action": "milestone", "index": "1",
                                  "csrf_token": csrf}).status_code == 200
    assert client.post(url, data={"action": "milestone", "index": "7",
                                  "csrf_token": csrf}).status_code == 400
    assert client.post(url, data={"action": "hide", "csrf_token": csrf}).status_code == 200
    deleted = client.post(url, data={"action": "delete", "csrf_token": csrf})
    assert deleted.status_code == 200 and deleted.text == ""
    assert client.post(url, data={"action": "взлом", "csrf_token": csrf}).status_code == 400
    assert client.post("/dossiers/1/999", data={"action": "hide",
                                                "csrf_token": csrf}).status_code == 404
    assert [name for name, _ in world["calls"]] == [
        "edit_dossier_summary", "delete_milestone", "delete_milestone", "set_dossier_hidden",
        "delete_dossier"]
    assert world["calls"][0][1] == (1, SASHA, "Новая")

    opened = client.post("/dossiers/settings/1", data={"field": "public", "value": "1",
                                                       "csrf_token": csrf})
    assert opened.status_code == 303 and world["settings"] == {"public": True}
    assert client.post("/dossiers/settings/1", data={"field": "чужое", "value": "1",
                                                     "csrf_token": csrf}).status_code == 400


def test_owner_starts_the_archive_backfill(world: dict[str, Any],
                                           monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    started: list[int] = []

    def launch(chat: Chat, day: date, **kw: Any) -> Any:
        started.append(chat.id)
        return SimpleNamespace(id="job-d", done=False, resumed=0, elapsed=2, title="досье",
                               note="3 из 37: 26.09", error="", result=None)

    monkeypatch.setattr(web_app, "launch_dossier_backfill", launch)
    client = client_as(OWNER)
    response = client.post("/dossiers/backfill/1", data={"csrf_token": csrf_of(client)})
    assert response.status_code == 200 and "день 3 из 37: 26.09" in response.text
    assert started == [1]


@pytest.mark.parametrize("method,path", [
    ("get", "/dossiers"), ("post", f"/dossiers/1/{SASHA}"), ("post", "/dossiers/settings/1"),
    ("post", "/dossiers/backfill/1"),
])
def test_dossiers_admin_is_owner_only(world: dict[str, Any], method: str, path: str):
    client = client_as(MEMBER)
    data = {"action": "delete", "field": "public", "value": "1", "csrf_token": csrf_of(client)}
    response = client.get(path) if method == "get" else client.post(path, data=data)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert world["calls"] == [] and world["settings"] == {}


def test_portal_has_no_dossiers_until_the_owner_opens_them(world: dict[str, Any]):
    member = client_as(MEMBER)
    assert member.get("/g/1/dossiers").status_code == 404
    assert client_as(OWNER).get("/g/1/dossiers").status_code == 404, "владелец видит как все"
    viewer = portal.Viewer(uid=MEMBER, is_owner=False)
    assert not portal.dossiers_visible(chat(), viewer)


def test_open_dossiers_on_the_portal(world: dict[str, Any]):
    world["settings"] = {"public": True}
    member = client_as(MEMBER)

    page = member.get("/g/1/dossiers")

    assert page.status_code == 200
    assert world["public_asked"][-1] is True, "без скрытых, /optout и пустых"
    assert "Королева хоррора" in page.text and "1 место и $7000" in page.text
    assert "Полина" not in page.text
    for owner_only in ("hx-post", "Удалить", "Скрыть", "Править"):
        assert owner_only not in page.text
    tabbar = page.text.split('class="tabbar"', 1)[1]
    assert "<span>Досье</span>" in tabbar and "<span>Полезное</span>" in tabbar


def test_portal_dossiers_need_the_full_mode(world: dict[str, Any]):
    world["settings"] = {"public": True}
    world["portal"] = "digests"
    assert client_as(MEMBER).get("/g/1/dossiers").status_code == 404
    world["portal"] = "off"
    assert client_as(MEMBER).get("/g/1/dossiers").status_code == 404
