"""База полезного: пополнение по итогам дня, поиск, модерация (решение владельца, 15.10).

Лайфхаки, решения и ответы на частые вопросы — нейросети, оплаты, деньги,
налоги — собираются из рабочих тем дня. Владелец архивирует лишнее в админке,
участники ищут на своей странице и видят только живые записи.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.db import repo
from src.db.models import Chat
from src.digest import knowledge as kb
from src.digest.render import Topic
from src.llm.client import Usage
from src.web import app as web_app
from src.web import auth, membership, portal

OWNER = 132036441
MEMBER = 555001
STRANGER = 555002
BOT_TOKEN = "123456:TEST-TOKEN"
DAY = date(2026, 10, 14)


def chat(**kw: Any) -> Chat:
    defaults: dict[str, Any] = {"id": 1, "tg_id": -100111, "title": "Нейросети",
                                "collect": True, "digest": True, "portal": "all"}
    return Chat(**{**defaults, **kw})


def topic(n: int, title: str, kind: str = "insight", summary: str = "",
          key: list[int] | None = None) -> Topic:
    return Topic(thread_id=n, title=title, kind=kind, summary=summary,
                 key_msg_ids=key or [], msg_count=6, participants=["Вася", "Петя"])


TOPICS = [
    topic(101, "Как платить за Midjourney из России", "resource",
          "Через карту Казахстана или сервис-посредник, комиссия около 10%.", key=[105]),
    topic(201, "Самозанятость и иностранные заказчики", "insight",
          "Чек в «Мой налог» на рубли по курсу ЦБ в день оплаты, ставка 6%."),
    topic(301, "Сватаем Милану", "fun", "Весь вечер шутили про свадьбу."),
    topic(401, "Пустой тред без пересказа", "insight"),
]


class KBLLM:
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
def kb_repo(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"existing": [], "added": [], "updated": []}

    async def list_knowledge(chat_id: int, **kw: Any) -> list[dict[str, Any]]:
        state["list_kw"] = kw
        return list(state["existing"])

    async def add_knowledge(chat_id: int, **kw: Any) -> int | None:
        state["added"].append(kw)
        return None if kw["title"] == "Уже есть" else len(state["added"])

    async def update_knowledge(knowledge_id: int, chat_id: int, **kw: Any) -> bool:
        state["updated"].append((knowledge_id, kw))
        return True

    monkeypatch.setattr(repo, "list_knowledge", list_knowledge)
    monkeypatch.setattr(repo, "add_knowledge", add_knowledge)
    monkeypatch.setattr(repo, "update_knowledge", update_knowledge)
    return state


# ============================================================ пополнение

def test_material_is_work_topics_with_a_summary():
    """Оффтоп — в «Жизнь чата», а не в базу; без пересказа нечего сохранять."""
    assert [t.thread_id for t in kb.material(TOPICS)] == [101, 201]


async def test_prompt_has_the_base_with_archive_marks_and_the_day(kb_repo: dict[str, Any]):
    kb_repo["existing"] = [
        {"id": 7, "category": "payments", "title": "Оплата Suno", "archived": False},
        {"id": 8, "category": "other", "title": "Где брать котиков", "archived": True},
    ]
    llm = KBLLM({"add": [], "update": []})

    await kb.update_knowledge(chat(), DAY, TOPICS, llm=llm)

    call = llm.calls[0]
    assert call["purpose"] == "knowledge"
    assert "14.10.2026" in call["system"] and "{day}" not in call["system"]
    assert "7. [payments] Оплата Suno" in call["user"]
    assert "8. [other] [архив] Где брать котиков" in call["user"], "архив видно — не добавит снова"
    assert kb_repo["list_kw"]["include_archived"] is True
    # в материал — только рабочие темы и их настоящие номера сообщений
    assert "Midjourney" in call["user"] and "[сообщения: 101, 105]" in call["user"]
    assert "Милану" not in call["user"] and "Пустой тред" not in call["user"]


async def test_answer_is_checked(kb_repo: dict[str, Any]):
    kb_repo["existing"] = [
        {"id": 7, "category": "payments", "title": "Оплата Suno", "archived": False},
        {"id": 8, "category": "other", "title": "Архивное", "archived": True},
    ]
    llm = KBLLM({
        "add": [
            {"title": "  Как оплатить Midjourney из России  ", "body": "Карта Казахстана.",
             "category": "PAYMENTS", "tags": ["Midjourney", "оплата", "midjourney", " "],
             "msg_ids": [101, 999, "105", "мусор"]},
            {"title": "Чек самозанятого за валюту", "body": "По курсу ЦБ.", "category": "налоги"},
            {"title": "Без ответа", "body": ""},
            {"title": "", "body": "Без вопроса"},
            "не словарь",
            {"title": "Уже есть", "body": "дубль"},
        ],
        "update": [
            {"id": 7, "body": "Теперь и через App Store.", "tags": ["App Store"]},
            {"id": 8, "body": "архив не трогаем"},
            {"id": 999, "body": "чужой номер"},
            {"id": "семь", "body": "мусор"},
        ],
    })

    added = await kb.update_knowledge(chat(), DAY, TOPICS, llm=llm)

    assert added == ["Как оплатить Midjourney из России", "Чек самозанятого за валюту"]
    first, second = kb_repo["added"][0], kb_repo["added"][1]
    assert first["category"] == "payments"
    assert first["tags"] == ["midjourney", "оплата"]
    assert first["sources"] == [{"day": "2026-10-14", "msg_id": 101},
                                {"day": "2026-10-14", "msg_id": 105}]
    assert second["category"] == "other", "неизвестный раздел — в «Разное»"
    assert second["sources"] == []
    assert [a["title"] for a in kb_repo["added"]] == [
        "Как оплатить Midjourney из России", "Чек самозанятого за валюту", "Уже есть",
    ]
    assert kb_repo["updated"] == [
        (7, {"day": DAY, "body": "Теперь и через App Store.", "tags": ["app store"]}),
    ], "дополняем только живые записи с настоящими номерами"


async def test_at_most_eight_new_entries_a_day(kb_repo: dict[str, Any]):
    llm = KBLLM({"add": [{"title": f"Лайфхак {i}", "body": "текст"} for i in range(20)]})
    added = await kb.update_knowledge(chat(), DAY, TOPICS, llm=llm)
    assert len(added) == kb.MAX_ADDS_PER_DAY


async def test_switched_off_or_nothing_useful_means_no_call(kb_repo: dict[str, Any]):
    llm = KBLLM({"add": [{"title": "x", "body": "y"}]})
    off = chat(settings={"knowledge": {"enabled": False}})

    assert await kb.update_knowledge(off, DAY, TOPICS, llm=llm) == []
    assert await kb.update_knowledge(chat(), DAY, [TOPICS[2], TOPICS[3]], llm=llm) == []
    assert llm.calls == []


async def test_model_failure_does_not_break_the_digest(kb_repo: dict[str, Any]):
    from src.digest import pipeline as dp

    result = dp.DigestResult(chat_tg_id=-100111, day=DAY, markdown="", topics=[],
                             usage=Usage(0, 0), msg_count=0, all_topics=TOPICS)
    llm = KBLLM(RuntimeError("модель легла"))

    assert await dp._update_knowledge(chat(), DAY, result, llm=llm) == []
    assert await kb.update_knowledge(chat(), DAY, TOPICS, llm=KBLLM("не JSON-объект")) == []


# ============================================================ страницы

@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # тот самый cfg, что у админки: test_config умеет перезагружать модуль настроек
    for conf in {id(auth.cfg): auth.cfg, id(web_app.cfg): web_app.cfg,
                 id(portal.cfg): portal.cfg}.values():
        monkeypatch.setattr(conf, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
        monkeypatch.setattr(conf, "BOT_TOKEN", BOT_TOKEN)
        monkeypatch.setattr(conf, "OWNER_ID", OWNER)
        monkeypatch.setattr(conf, "BOT_USERNAME", "temabot")
        monkeypatch.setattr(conf, "WEB_BASE_URL", "https://admin.example.com")
        monkeypatch.setattr(conf, "DATABASE_URL", "")
    membership.forget()


def client_as(user_id: int | None) -> TestClient:
    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    if user_id is not None:
        client.cookies.set(auth.COOKIE_NAME, auth.issue_session(user_id))
    return client


def csrf_of(client: TestClient) -> str:
    return auth.read_session(client.cookies.get(auth.COOKIE_NAME))["csrf"]


ROWS = [
    {"id": 1, "chat_id": 1, "title": "Как оплатить <script>Midjourney</script>",
     "body": "Карта Казахстана.\nИли посредник.", "category": "payments",
     "tags": ["midjourney", "оплата"], "sources": [{"day": "2026-10-14", "msg_id": 101}],
     "first_day": DAY, "last_day": DAY, "mentions": 1, "archived": False},
    {"id": 2, "chat_id": 1, "title": "Старый способ оплаты", "body": "Больше не работает.",
     "category": "payments", "tags": [], "sources": [], "first_day": DAY, "last_day": DAY,
     "mentions": 1, "archived": True},
]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"portal": "all", "searches": [], "archived": [], "edited": [],
                             "deleted": [], "settings": []}

    async def list_chats(**kw: Any) -> list[Chat]:
        return [chat(portal=state["portal"])]

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return chat(portal=state["portal"]) if chat_id == 1 else None

    async def search_knowledge(chat_id: int, query: str, **kw: Any) -> list[dict[str, Any]]:
        state["searches"].append({"chat_id": chat_id, "query": query, **kw})
        rows = ROWS if kw.get("include_archived") else [r for r in ROWS if not r["archived"]]
        return [dict(r) for r in rows]

    async def get_knowledge(knowledge_id: int) -> dict[str, Any] | None:
        return next((dict(r) for r in ROWS if r["id"] == knowledge_id), None)

    async def set_knowledge_archived(knowledge_id: int, archived: bool) -> dict[str, Any]:
        state["archived"].append((knowledge_id, archived))
        return {**ROWS[0], "id": knowledge_id, "archived": archived}

    async def edit_knowledge(knowledge_id: int, **kw: Any) -> dict[str, Any]:
        state["edited"].append((knowledge_id, kw))
        if kw["title"] == "Занято":
            raise ValueError("запись с таким вопросом уже есть")
        return {**ROWS[0], **kw, "id": knowledge_id}

    async def delete_knowledge(knowledge_id: int) -> bool:
        state["deleted"].append(knowledge_id)
        return True

    async def update_chat_settings(chat_id: int, patch: dict[str, Any]) -> None:
        state["settings"].append((chat_id, patch))

    async def is_member(chat: Chat, user_id: int, **kw: Any) -> bool:
        return user_id in (OWNER, MEMBER)

    for name, fn in {
        "list_chats": list_chats, "get_chat_by_id": get_chat_by_id,
        "search_knowledge": search_knowledge, "get_knowledge": get_knowledge,
        "set_knowledge_archived": set_knowledge_archived, "edit_knowledge": edit_knowledge,
        "delete_knowledge": delete_knowledge, "update_chat_settings": update_chat_settings,
    }.items():
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(membership, "is_member", is_member)
    return state


def test_owner_searches_and_moderates(world: dict[str, Any]):
    page = client_as(OWNER).get("/kb", params={"q": "оплата", "cat": "payments"})

    assert page.status_code == 200
    assert world["searches"][-1] == {"chat_id": 1, "query": "оплата", "category": "payments",
                                     "include_archived": False, "limit": 200}
    assert "&lt;script&gt;Midjourney" in page.text and "<script>Midjourney" not in page.text
    assert "В архив" in page.text and "Править" in page.text
    assert "Старый способ оплаты" not in page.text, "архив — отдельной кнопкой"


def test_owner_archive_view_shows_only_the_archive(world: dict[str, Any]):
    page = client_as(OWNER).get("/kb", params={"archived": 1, "cat": "взлом"})

    assert world["searches"][-1]["include_archived"] is True
    assert world["searches"][-1]["category"] == "", "неизвестный раздел не уходит в запрос"
    assert "Старый способ оплаты" in page.text and "Из архива" in page.text
    assert "Карта Казахстана" not in page.text


def test_owner_actions(world: dict[str, Any]):
    client = client_as(OWNER)
    csrf = csrf_of(client)

    archived = client.post("/kb/1", data={"action": "archive", "csrf_token": csrf})
    assert archived.status_code == 200 and world["archived"] == [(1, True)]
    assert "в архиве" in archived.text and "Из архива" in archived.text

    edited = client.post("/kb/1", data={
        "action": "edit", "csrf_token": csrf, "title": " Оплата MJ ", "body": "Карта.",
        "category": "непонятно", "tags": "MJ, оплата, ,карта",
    })
    assert edited.status_code == 200
    assert world["edited"][-1] == (1, {"title": "Оплата MJ", "body": "Карта.",
                                       "category": "other", "tags": ["mj", "оплата", "карта"]})

    clash = client.post("/kb/1", data={"action": "edit", "csrf_token": csrf,
                                       "title": "Занято", "body": "x"})
    assert clash.status_code == 400
    assert client.post("/kb/1", data={"action": "edit", "csrf_token": csrf,
                                      "title": "Пусто", "body": " "}).status_code == 400
    assert client.post("/kb/1", data={"action": "взлом", "csrf_token": csrf}).status_code == 400
    assert client.post("/kb/77", data={"action": "archive", "csrf_token": csrf}).status_code == 404

    deleted = client.post("/kb/2", data={"action": "delete", "csrf_token": csrf})
    assert deleted.status_code == 200 and deleted.text == "" and world["deleted"] == [2]

    off = client.post("/kb/settings/1", data={"enabled": "0", "csrf_token": csrf})
    assert off.status_code == 303 and world["settings"] == [(1, {"knowledge": {"enabled": False}})]


def test_owner_actions_need_csrf(world: dict[str, Any]):
    client = client_as(OWNER)
    response = client.post("/kb/1", data={"action": "delete", "csrf_token": "мимо"})
    assert response.status_code == 403 and world["deleted"] == []


@pytest.mark.parametrize("path,data", [
    ("/kb/1", {"action": "delete"}), ("/kb/1", {"action": "archive"}),
    ("/kb/settings/1", {"enabled": "0"}),
])
def test_member_cannot_moderate(world: dict[str, Any], path: str, data: dict[str, str]):
    client = client_as(MEMBER)
    response = client.post(path, data={**data, "csrf_token": csrf_of(client)})

    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert world["deleted"] == [] and world["archived"] == [] and world["settings"] == []


def test_member_cookie_does_not_open_the_admin_base(world: dict[str, Any]):
    response = client_as(MEMBER).get("/kb")
    assert response.status_code == 303 and response.headers["location"] == "/login"


@pytest.mark.parametrize("mode", ["digests", "all"])
def test_member_searches_on_the_portal(world: dict[str, Any], mode: str):
    world["portal"] = mode
    page = client_as(MEMBER).get("/g/1/kb", params={"q": " оплата ", "cat": "taxes"})

    assert page.status_code == 200
    assert world["searches"][-1] == {"chat_id": 1, "query": "оплата", "category": "taxes",
                                     "limit": 100}, "архив участникам не ищется"
    assert "Карта Казахстана" in page.text and "Старый способ оплаты" not in page.text
    assert "&lt;script&gt;" in page.text and "<script>Midjourney" not in page.text
    # только чтение: ни кнопок модерации, ни правки
    for owner_only in ("В архив", "Из архива", "Удалить", "Править", "hx-post"):
        assert owner_only not in page.text, owner_only
    assert "Полезное" in page.text.split('class="tabbar"', 1)[1]


def test_owner_on_the_portal_sees_it_read_only_too(world: dict[str, Any]):
    page = client_as(OWNER).get("/g/1/kb")
    assert page.status_code == 200 and "В архив" not in page.text and "hx-post" not in page.text


def test_portal_base_is_closed_to_strangers_and_when_off(world: dict[str, Any]):
    assert client_as(STRANGER).get("/g/1/kb").status_code == 404
    assert client_as(MEMBER).get("/g/2/kb").status_code == 404
    world["portal"] = "off"
    assert client_as(MEMBER).get("/g/1/kb").status_code == 404
    assert client_as(None).get("/g/1/kb").status_code == 303


# ============================================================ настоящая база

@pytest.mark.usefixtures("db")
async def test_knowledge_in_the_real_database():
    stored = await repo.bootstrap_primary_chat(-100111)
    other = await repo.bootstrap_primary_chat(-100222)
    first = await repo.add_knowledge(
        stored.id, title="Как платить налоги самозанятому", body="Чек в «Мой налог», ставка 6%.",
        category="taxes", tags=["нпд", "самозанятость"], day=date(2026, 10, 1),
        sources=[{"day": "2026-10-01", "msg_id": 11}],
    )
    dup = await repo.add_knowledge(
        stored.id, title="как платить НАЛОГИ самозанятому", body="другое", category="taxes",
        tags=[], day=DAY,
    )
    suno = await repo.add_knowledge(
        stored.id, title="Оплата Suno", body="Через App Store с турецким аккаунтом.",
        category="payments", tags=["suno"], day=DAY,
    )
    await repo.add_knowledge(other.id, title="Чужой налог", body="налоги другой группы",
                             category="taxes", tags=[], day=DAY)

    assert first is not None and suno is not None and dup is None

    async def found(query: str, **kw: Any) -> list[str]:
        return [r["title"] for r in await repo.search_knowledge(stored.id, query, **kw)]

    assert await found("налогов") == ["Как платить налоги самозанятому"], "морфология"
    assert await found("НПД") == ["Как платить налоги самозанятому"], "метка"
    assert await found("App Store") == ["Оплата Suno"], "вхождение подстроки"
    assert await found("", category="payments") == ["Оплата Suno"]
    assert sorted(await found("")) == ["Как платить налоги самозанятому", "Оплата Suno"]
    # запрос параметризован: кавычки и «or» — просто слова, чужая группа не всплывает
    assert "Чужой налог" not in await found("налог' or 1=1 --")

    # дополнение: метки — объединение, упоминание — раз в новый день
    assert await repo.update_knowledge(first, stored.id, day=DAY, body="", tags=["ип", "нпд"],
                                       sources=[{"day": DAY.isoformat(), "msg_id": 12}])
    await repo.update_knowledge(first, stored.id, day=DAY, body="Новое описание.")
    row = await repo.get_knowledge(first)
    assert row is not None
    assert sorted(row["tags"]) == ["ип", "нпд", "самозанятость"]
    assert row["mentions"] == 2 and row["last_day"] == DAY and row["body"] == "Новое описание."
    assert [s["msg_id"] for s in row["sources"]] == [11, 12]
    assert not await repo.update_knowledge(first, other.id, day=DAY, body="чужая группа")

    # архив: не ищется участникам, модель его не дополняет
    await repo.set_knowledge_archived(suno, True)
    assert await found("Suno") == []
    assert await found("Suno", include_archived=True) == ["Оплата Suno"]
    assert not await repo.update_knowledge(suno, stored.id, day=DAY, body="поверх архива")
    assert [r["title"] for r in await repo.list_knowledge(stored.id)] == [
        "Как платить налоги самозанятому"]

    with pytest.raises(ValueError):
        await repo.edit_knowledge(suno, title="КАК ПЛАТИТЬ НАЛОГИ САМОЗАНЯТОМУ", body="x",
                                  category="taxes", tags=[])
    edited = await repo.edit_knowledge(suno, title="Оплата Suno картой", body="x",
                                       category="payments", tags=["suno"])
    assert edited is not None and edited["title"] == "Оплата Suno картой"
    assert await repo.delete_knowledge(suno) is True
    assert await repo.get_knowledge(suno) is None
