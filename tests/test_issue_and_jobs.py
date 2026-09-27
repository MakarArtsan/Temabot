"""Прогон в фоне, самопроверка бота, выпуск-статья с коротким постом, разделы участников."""
from __future__ import annotations

import asyncio
import time
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.bot import handlers_copier as copier
from src.bot import main as bot_main
from src.bot import middlewares
from src.config import cfg
from src.db.models import Chat, Digest
from src.digest import pipeline as dp
from src.digest import publish
from src.digest.render import HASHTAG, DigestData, Topic, teaser_html
from src.llm.client import Usage
from src.web import app as web_app
from src.web import auth, jobs, membership

OWNER = 132036441
MEMBER = 555001
DAY = date(2026, 9, 24)


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # в полном прогоне другие тесты перечитывают конфиг — патчим те объекты, что видит код
    for target in {id(cfg): cfg, id(auth.cfg): auth.cfg, id(web_app.cfg): web_app.cfg,
                   id(publish.cfg): publish.cfg}.values():
        monkeypatch.setattr(target, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
        monkeypatch.setattr(target, "OWNER_ID", OWNER)
        monkeypatch.setattr(target, "BOT_USERNAME", "temabot")
        monkeypatch.setattr(target, "WEB_BASE_URL", "https://site.example.com/")
        monkeypatch.setattr(target, "DATABASE_URL", "")
    membership.forget()
    jobs.clear()


def client_as(user_id: int | None) -> TestClient:
    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    if user_id is not None:
        client.cookies.set(auth.COOKIE_NAME, auth.issue_session(user_id))
    return client


def csrf_of(client: TestClient) -> str:
    return str(auth.read_session(client.cookies.get(auth.COOKIE_NAME))["csrf"])


def topics() -> list[Topic]:
    return [
        Topic(thread_id=11, title="Цены на генерацию", kind="insight", takeaway="12 ₽ за ролик",
              summary="Вася посчитал, что Veo выходит в 12 ₽.", msg_count=23,
              participants=["Вася", "Петя"], key_msg_ids=[4410],
              sides=[{"who": "Вася", "stance": "за Veo"}, {"who": "Петя", "stance": "за Kling"}]),
        Topic(thread_id=12, title="Маша переезжает", kind="life",
              summary="Маша с октября в Питере.", msg_count=9, participants=["Маша"],
              key_msg_ids=[4500]),
    ]


ARTICLE = {
    "headline": "Veo подешевел — и чат <b>взорвался</b>",
    "lead": "Главное за среду: цены, споры и один переезд.",
    "teaser": ["Почему Вася снова за Veo", "Маша едет в Питер"],
    "stories": [
        {"thread_id": 11, "kicker": "Деньги", "headline": "12 рублей за ролик",
         "hook": "Вася пересчитал всё <script>x</script>", "text": "Абзац один.\n\nАбзац два."},
        {"thread_id": 12, "kicker": "Люди", "headline": "Питер ждёт",
         "hook": "", "text": "Маша переезжает."},
    ],
}


def digest_data(**kw: Any) -> DigestData:
    defaults: dict[str, Any] = dict(
        chat_tg_id=-100111, day=DAY, chat_title="Нейросети",
        highlights=["Veo подешевел"], topics=topics(), msg_count=86, participants=7,
        heroes="🏅 Герои дня: 🧠 Петя", article=ARTICLE,
    )
    defaults.update(kw)
    return DigestData(**defaults)


# ================================================================ фоновые задачи

async def test_job_runs_in_background_and_is_not_started_twice():
    gate = asyncio.Event()
    calls = []

    async def action() -> int:
        calls.append(1)
        await gate.wait()
        return 42

    job = jobs.start("digest:1:2026-09-24", "дайджест", action)
    again = jobs.start("digest:1:2026-09-24", "дайджест", action)
    assert again is job and not job.done

    gate.set()
    assert job.task is not None
    await job.task
    assert job.done and job.result == 42 and not job.error
    assert len(calls) == 1
    assert jobs.get(job.id) is job


async def test_failed_job_keeps_the_error():
    async def action() -> None:
        raise RuntimeError("модель не ответила")

    job = jobs.start("preview:1", "прогон", action)
    assert job.task is not None
    await job.task
    assert job.done and job.error == "модель не ответила"


def _admin_world(
    monkeypatch: pytest.MonkeyPatch, *, build_delay: float = 0.0,
    stored: Digest | None = None, llm_down: bool = False,
) -> list[Any]:
    chat = Chat(id=1, tg_id=-100111, title="Нейросети", collect=True, digest=True)
    builds: list[Any] = []

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return chat if chat_id == 1 else None

    async def get_digest(chat_id: int, day: date) -> Digest | None:
        return stored

    async def build_digest(chat: Chat, day: date, **kw: Any) -> Any:
        builds.append(day)
        await asyncio.sleep(build_delay)
        return SimpleNamespace(
            usage=Usage(tokens_in=100, tokens_out=50), all_topics=topics(),
            markdown="*Дайджест*", data=digest_data(), digest_id=None, llm_is_down=llm_down,
        )

    async def save_result(chat: Chat, day: date, result: Any, **kw: Any) -> int:
        builds.append(("archive", day))
        result.digest_id = 77
        return 77

    async def run_for_chat(chat: Chat, day: date, *, save: bool = True) -> Any:
        builds.append(("save", day, save))
        return SimpleNamespace(digest_id=77)

    monkeypatch.setattr(web_app.repo, "get_chat_by_id", get_chat_by_id)
    monkeypatch.setattr(web_app.repo, "get_digest", get_digest)
    monkeypatch.setattr(dp, "build_digest", build_digest)
    monkeypatch.setattr(dp, "run_for_chat", run_for_chat)
    monkeypatch.setattr(dp, "save_result", save_result)
    return builds


def _poll(client: TestClient, url: str, done_marker: str) -> str:
    for _ in range(100):
        page = client.get(url)
        assert page.status_code == 200
        if done_marker in page.text:
            return page.text
        time.sleep(0.02)
    raise AssertionError("задача так и не завершилась")


def test_preview_answers_at_once_and_page_polls_for_the_result(monkeypatch: pytest.MonkeyPatch):
    """Прогон за прошлый день больше не держит запрос минутами (прокси его рвал)."""
    builds = _admin_world(monkeypatch, build_delay=0.2)
    with client_as(OWNER) as client:
        started = time.monotonic()
        page = client.post("/selection/1/preview",
                           data={"day": "2026-09-24", "csrf_token": csrf_of(client)})
        assert page.status_code == 200
        assert time.monotonic() - started < 0.2
        assert 'hx-get="/jobs/' in page.text and "view=preview" in page.text

        job_id = page.text.split('hx-get="/jobs/')[1].split("?")[0]
        result = _poll(client, f"/jobs/{job_id}?view=preview", "Цены на генерацию")

    assert builds == [DAY, ("archive", DAY)]   # за день было пусто — прогон лёг в архив
    assert "hx-get" not in result          # готово — опрос прекращается
    assert 'href="/digests/77"' in result and "Заменить в архиве" not in result


def _run_preview(client: TestClient) -> tuple[str, str]:
    page = client.post("/selection/1/preview",
                       data={"day": "2026-09-24", "csrf_token": csrf_of(client)})
    job_id = page.text.split('hx-get="/jobs/')[1].split("?")[0]
    return job_id, _poll(client, f"/jobs/{job_id}?view=preview", "Цены на генерацию")


def test_preview_never_silently_replaces_a_stored_digest(monkeypatch: pytest.MonkeyPatch):
    """Прежний дайджест дня заменяется только по кнопке: с ним уходят оценки тем."""
    stored = Digest(id=5, chat_id=1, day=DAY, summary_md="*Старый*", payload={}, msg_count=80)
    builds = _admin_world(monkeypatch, stored=stored)
    with client_as(OWNER) as client:
        job_id, result = _run_preview(client)
        assert builds == [DAY]
        assert "Заменить в архиве" in result and 'href="/digests/5"' in result
        assert f'hx-post="/jobs/{job_id}/save"' in result

        for _ in range(2):   # второй клик не пишет ещё раз
            saved = client.post(f"/jobs/{job_id}/save", data={"csrf_token": csrf_of(client)})
            assert saved.status_code == 200 and 'href="/digests/77"' in saved.text
        # и повторный показ прогона помнит, что он уже в архиве
        again = client.get(f"/jobs/{job_id}?view=preview").text
        assert "Заменить в архиве" not in again and 'href="/digests/77"' in again

    assert builds == [DAY, ("archive", DAY)]   # модель второй раз не звали


def test_preview_with_model_down_is_not_archived(monkeypatch: pytest.MonkeyPatch):
    builds = _admin_world(monkeypatch, llm_down=True)
    with client_as(OWNER) as client:
        job_id, result = _run_preview(client)
        assert "не сохраняю" in result
        saved = client.post(f"/jobs/{job_id}/save", data={"csrf_token": csrf_of(client)})
        assert "не ответила" in saved.text
    assert builds == [DAY]


def test_saving_a_preview_is_owner_only_and_needs_csrf(monkeypatch: pytest.MonkeyPatch):
    stored = Digest(id=5, chat_id=1, day=DAY, summary_md="", payload={}, msg_count=80)
    builds = _admin_world(monkeypatch, stored=stored)
    with client_as(OWNER) as owner:
        job_id, _ = _run_preview(owner)
        assert owner.post(f"/jobs/{job_id}/save", data={"csrf_token": "чужой"}).status_code == 403
        member = client_as(MEMBER)
        response = member.post(f"/jobs/{job_id}/save", data={"csrf_token": csrf_of(member)})
        assert response.status_code == 303
    assert builds == [DAY]


def test_bad_preview_date_is_reported_without_a_job(monkeypatch: pytest.MonkeyPatch):
    _admin_world(monkeypatch)
    client = client_as(OWNER)
    page = client.post("/selection/1/preview", data={"day": "24.09", "csrf_token": csrf_of(client)})
    assert page.status_code == 200 and "2026-09-20" in page.text


def test_manual_build_goes_to_background_and_links_the_digest(monkeypatch: pytest.MonkeyPatch):
    builds = _admin_world(monkeypatch)

    async def get_states() -> dict[str, Any]:
        return {}

    async def list_chats(**kw: Any) -> list[Chat]:
        return []

    async def pending_media_count() -> int:
        return 0

    monkeypatch.setattr(web_app.repo, "get_states", get_states)
    monkeypatch.setattr(web_app.repo, "list_chats", list_chats)
    monkeypatch.setattr(web_app.repo, "pending_media_count", pending_media_count)

    with client_as(OWNER) as client:
        response = client.post("/system/digest", data={
            "chat_id": "1", "day": "2026-09-24", "csrf_token": csrf_of(client)})
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/system?job=")
        job_id = location.split("=", 1)[1]

        assert "дайджест за 24.09" in client.get(location).text
        _poll(client, f"/jobs/{job_id}", "/digests/77")

    assert builds == [("save", DAY, True)]


def test_job_status_is_owner_only(monkeypatch: pytest.MonkeyPatch):
    _admin_world(monkeypatch)

    async def nothing() -> None:
        return None

    async def make() -> str:
        return jobs.start("x", "x", nothing).id

    job_id = asyncio.run(make())
    for who in (None, MEMBER):
        response = client_as(who).get(f"/jobs/{job_id}")
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_unknown_job_says_so():
    page = client_as(OWNER).get("/jobs/нет-такой")
    assert page.status_code == 200 and "не найдена" in page.text


# ============================================================ самопроверка бота

class FakeBot:
    def __init__(self, url: str = "") -> None:
        self.url = url
        self.deleted: list[bool] = []

    async def get_webhook_info(self) -> Any:
        return SimpleNamespace(url=self.url, last_error_message="Connection refused")

    async def delete_webhook(self, *, drop_pending_updates: bool) -> bool:
        self.deleted.append(drop_pending_updates)
        return True


async def test_leftover_webhook_is_removed_without_dropping_updates(
    monkeypatch: pytest.MonkeyPatch,
):
    """Вебхук от прежнего хостинга забирал входящие: бот не слышал упоминаний."""
    saved: dict[str, Any] = {}

    async def set_state(key: str, value: Any) -> None:
        saved[key] = value

    monkeypatch.setattr(bot_main.cfg, "DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(bot_main.repo, "set_state", set_state)
    bot = FakeBot("https://old-host.example.com/webhook/secret-path")

    await bot_main.check_updates_channel(bot)

    assert bot.deleted == [False]
    assert saved[bot_main.WEBHOOK_KEY]["removed"] == "old-host.example.com"
    assert "secret-path" not in str(saved)


async def test_no_webhook_means_nothing_to_do():
    bot = FakeBot("")
    await bot_main.check_updates_channel(bot)
    assert bot.deleted == []


async def test_incoming_updates_are_marked_not_too_often(monkeypatch: pytest.MonkeyPatch):
    marks: list[str] = []

    async def set_state(key: str, value: Any) -> None:
        marks.append(key)

    monkeypatch.setattr(middlewares.repo, "set_state", set_state)
    mark = middlewares.LastUpdateMark(every_sec=60)
    handled = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        handled.append(event)
        return "ok"

    assert await mark(handler, "u1", {}) == "ok"          # type: ignore[arg-type]
    assert await mark(handler, "u2", {}) == "ok"          # type: ignore[arg-type]
    assert marks == [middlewares.LAST_UPDATE_KEY]
    assert handled == ["u1", "u2"]


async def test_mark_failure_does_not_block_the_update(monkeypatch: pytest.MonkeyPatch):
    async def broken(*a: Any) -> None:
        raise ConnectionError("БД недоступна")

    monkeypatch.setattr(middlewares.repo, "set_state", broken)

    async def handler(event: Any, data: dict[str, Any]) -> str:
        return "ok"

    assert await middlewares.LastUpdateMark()(handler, "u", {}) == "ok"  # type: ignore[arg-type]


async def test_telegraph_outage_does_not_stop_the_bot(monkeypatch: pytest.MonkeyPatch):
    """Раньше сбой Telegraph при старте ронял бота целиком — копировщик молчал."""
    class Me:
        async def get_me(self) -> Any:
            return SimpleNamespace(username="temabot")

    async def broken() -> Any:
        raise ConnectionError("telegra.ph недоступен")

    monkeypatch.setattr(copier, "_telegraph", None)
    monkeypatch.setattr(copier, "_telegraph_client", broken)

    await copier.init_copier(Me())  # type: ignore[arg-type]

    assert copier._bot_username == "temabot"


# ====================================================== выпуск-статья и пост

def test_article_keeps_only_real_topics():
    raw = {
        "headline": "  Заголовок дня  ",
        "lead": "Лид",
        "teaser": ["раз", "", "два"],
        "stories": [
            {"thread_id": "11", "headline": "Про цены", "text": "т"},
            {"thread_id": 11, "headline": "Дубль", "text": "т"},
            {"thread_id": 999, "headline": "Выдумка", "text": "т"},
            {"thread_id": 12, "headline": "", "text": "без заголовка"},
            "мусор",
        ],
    }
    article = dp.clean_article(raw, topics())

    assert article["headline"] == "Заголовок дня"
    assert article["teaser"] == ["раз", "два"]
    assert [s["headline"] for s in article["stories"]] == ["Про цены"]
    assert dp.clean_article({"headline": ""}, topics()) == {}
    assert dp.clean_article(["не словарь"], topics()) == {}


async def test_article_failure_leaves_the_digest_alone():
    async def broken(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("модель легла")

    chat = Chat(id=1, tg_id=-100111, title="Нейросети")
    article, usage = await dp.make_article(topics(), ["x"], chat, DAY, llm=broken)
    assert article == {} and usage.tokens_in == 0

    empty, _ = await dp.make_article([], [], chat, DAY, llm=broken)
    assert empty == {}


async def test_article_prompt_carries_topics_and_sides():
    seen: dict[str, Any] = {}

    async def llm(messages: list[dict[str, str]], **kw: Any) -> Any:
        seen.update(kw, user=messages[1]["content"])
        return ARTICLE, Usage(tokens_in=10, tokens_out=5)

    chat = Chat(id=1, tg_id=-100111, title="Нейросети")
    article, usage = await dp.make_article(topics(), ["Veo подешевел"], chat, DAY, llm=llm)

    assert seen["purpose"] == "article" and seen["chat_id"] == 1
    assert "11. [insight] Цены на генерацию" in seen["user"]
    assert "Вася — за Veo" in seen["user"]
    assert len(article["stories"]) == 2 and usage.tokens_in == 10


def test_article_survives_storage():
    data = digest_data()
    assert DigestData.from_dict(data.to_dict()).article == ARTICLE


URL = "https://site.example.com/g/1/d/2026-09-24"


def test_short_post_reads_like_a_magazine_announcement():
    """«Выпуск #3: …» ссылкой, абзац о главном с именами, «Плюс …», ссылка, #дайджест."""
    article = {**ARTICLE,
               "post": "Вася посчитал, что Veo выходит в 12 ₽ за ролик, а Маша едет в Питер.",
               "also": ["липсинг", "Draft-мод", "танцы на столе"]}
    post = teaser_html(digest_data(article=article), URL, number=3)

    assert post.startswith(
        f'<b><a href="{URL}">Выпуск #3: Veo подешевел — и чат &lt;b&gt;взорвался&lt;/b&gt;</a></b>'
    )
    assert ("Вася посчитал, что Veo выходит в 12 ₽ за ролик, а Маша едет в Питер. "
            "Плюс липсинг, Draft-мод и танцы на столе.") in post
    assert f'<a href="{URL}">Читать выпуск на сайте →</a>' in post
    assert post.rstrip().endswith(HASHTAG)
    # один компактный пост: ни подробностей тем, ни списка, ни рейтингов, ни голых адресов
    assert "Абзац" not in post and "▸" not in post and "Герои" not in post
    assert post.count("https://") == 2 and ">https://" not in post
    assert len(post) < 1024


def test_old_article_without_post_uses_the_lead():
    post = teaser_html(digest_data(), URL, number=1)
    assert "Главное за среду: цены, споры и один переезд." in post and "Плюс" not in post


def test_no_article_builds_the_announcement_from_topics():
    many = [Topic(thread_id=n, title=f"Тема {n}", kind="insight", msg_count=5)
            for n in range(1, 10)]
    data = digest_data(topics=many, article={}, highlights=["Главное раз", "Главное два."])
    post = teaser_html(data, URL, number=2)
    assert "Выпуск #2: Тема 1, Тема 2 и Тема 3</a>" in post
    assert "Главное раз. Главное два. Плюс Тема 4, Тема 5, Тема 6, Тема 7 и Тема 8." in post


def test_work_topics_lead_the_announcement():
    post = teaser_html(digest_data(article={}), URL)
    assert "Цены на генерацию и Маша переезжает</a>" in post


@pytest.mark.parametrize(
    ("portal", "fmt", "base", "expected"),
    [
        ("digests", None, "https://site.example.com/", URL),
        ("all", "short", "https://site.example.com", URL),
        ("off", None, "https://site.example.com", ""),      # участники ссылку не откроют
        ("all", "full", "https://site.example.com", ""),    # владелец выбрал «целиком»
        ("all", None, "", ""),                              # адрес сайта неизвестен
    ],
)
def test_article_link_only_when_members_can_open_it(
    portal: str, fmt: str | None, base: str, expected: str,
):
    settings = {"publish_format": fmt} if fmt else {}
    chat = Chat(id=1, tg_id=-100111, portal=portal, settings=settings)
    assert publish.article_url(chat, DAY, base) == expected


def test_group_gets_short_post_or_full_digest():
    short = publish.group_parts(digest_data(), ratings_public=False, url=URL, number=4)
    assert len(short) == 1 and HASHTAG in short[0] and "Выпуск #4" in short[0]

    full = publish.group_parts(digest_data(), ratings_public=False, url="")
    assert HASHTAG not in "".join(full) and "Цены на генерацию" in "".join(full)
    assert "Герои" not in "".join(full)


async def test_site_address_is_remembered_when_env_is_empty(monkeypatch: pytest.MonkeyPatch):
    """На Amvera WEB_BASE_URL не задан — короткий пост всё равно получает ссылку."""
    monkeypatch.setattr(publish.cfg, "WEB_BASE_URL", "")

    async def get_state(key: str, default: Any = None) -> Any:
        assert key == publish.SITE_URL_KEY
        return {"url": "https://temabot.example.app/"}

    async def number(chat_id: int, day: date) -> int:
        return 7

    monkeypatch.setattr(publish.repo, "get_state", get_state)
    monkeypatch.setattr(publish.repo, "digest_issue_number", number)
    chat = Chat(id=1, tg_id=-100111, portal="all", settings={})

    parts = await publish.post_for_group(digest_data(), chat)
    assert len(parts) == 1
    assert "https://temabot.example.app/g/1/d/2026-09-24" in parts[0] and "Выпуск #7" in parts[0]
    assert await publish.full_post_reason(chat) == ""


async def test_admin_explains_why_the_full_digest_goes_out(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(publish.cfg, "WEB_BASE_URL", "")

    async def nothing(key: str, default: Any = None) -> Any:
        return None

    monkeypatch.setattr(publish.repo, "get_state", nothing)
    assert "адрес сайта" in await publish.full_post_reason(Chat(id=1, tg_id=1, portal="all"))
    assert "закрыта" in await publish.full_post_reason(Chat(id=1, tg_id=1, portal="off"))
    full = Chat(id=1, tg_id=1, portal="all", settings={"publish_format": "full"})
    assert "целиком" in await publish.full_post_reason(full)


def test_site_is_remembered_only_from_the_owner(monkeypatch: pytest.MonkeyPatch):
    """Подделанный Host от постороннего не должен стать адресом в посте."""
    saved: list[Any] = []

    async def set_state(key: str, value: Any) -> None:
        saved.append(value)

    monkeypatch.setattr(web_app.cfg, "WEB_BASE_URL", "")
    monkeypatch.setattr(web_app.cfg, "DATABASE_URL", "postgresql://x@127.0.0.1:1/none")
    monkeypatch.setattr(web_app.repo, "set_state", set_state)
    monkeypatch.setattr(web_app, "_remembered_site", "")
    headers = {"host": "evil.example.com", "x-forwarded-proto": "https"}

    client_as(None).get("/login", headers=headers)
    client_as(MEMBER).get("/login", headers=headers)
    assert saved == []

    client_as(OWNER).get("/login", headers={"host": "temabot.example.app",
                                            "x-forwarded-proto": "https"})
    assert saved and saved[-1]["url"] == "https://temabot.example.app"


def test_owner_picks_publish_format(monkeypatch: pytest.MonkeyPatch):
    chat = Chat(id=1, tg_id=-100111, title="Нейросети")
    saved: list[Any] = []

    async def get_chat_by_tg_id(tg_id: int) -> Chat | None:
        return chat

    async def update_chat_settings(chat_id: int, patch: dict[str, Any]) -> None:
        saved.append((chat_id, patch))

    monkeypatch.setattr(web_app.repo, "get_chat_by_tg_id", get_chat_by_tg_id)
    monkeypatch.setattr(web_app.repo, "update_chat_settings", update_chat_settings)

    client = client_as(OWNER)
    bad = client.post("/groups/-100111", data={
        "field": "publish_format", "value": "всё", "csrf_token": csrf_of(client)})
    assert bad.status_code == 400
    client.post("/groups/-100111", data={
        "field": "publish_format", "value": "full", "csrf_token": csrf_of(client)})
    assert saved == [(1, {"publish_format": "full"})]


# ================================================================ раздел участников

@pytest.fixture
def portal_world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"portal": "all"}

    def chat() -> Chat:
        return Chat(id=1, tg_id=-100111, title="Нейросети", collect=True, digest=True,
                    portal=state["portal"])

    digest = Digest(id=5, chat_id=1, day=DAY, summary_md="", payload=digest_data().to_dict(),
                    msg_count=86)

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return chat() if chat_id == 1 else None

    async def list_portal_chats() -> list[Chat]:
        return [chat()] if state["portal"] != "off" else []

    async def list_chats(**kw: Any) -> list[Chat]:
        return [chat()]

    async def list_chat_digests(chat_id: int, limit: int = 60) -> list[Digest]:
        return [digest]

    async def get_digest(chat_id: int, day: date) -> Digest | None:
        return digest if day == DAY else None

    async def messages_per_day(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"day": DAY, "count": 86}]

    async def get_author_stats(chat_id: Any, **kw: Any) -> list[dict[str, Any]]:
        return []

    async def list_lore(chat_id: int, **kw: Any) -> list[dict[str, Any]]:
        return [
            {"id": 1, "kind": "meme", "title": "«Сделай красиво»", "body": "вечная просьба",
             "tg_user_id": None, "person": None, "mentions": 3},
            {"id": 2, "kind": "role", "title": "Главный скептик", "body": "просит пруфы",
             "tg_user_id": 7, "person": "Петя", "mentions": 5},
        ]

    async def is_member(chat: Chat, user_id: int, **kw: Any) -> bool:
        return user_id in (OWNER, MEMBER)

    for name, fn in {
        "get_chat_by_id": get_chat_by_id, "list_portal_chats": list_portal_chats,
        "list_chats": list_chats, "list_chat_digests": list_chat_digests,
        "get_digest": get_digest, "messages_per_day": messages_per_day,
        "get_author_stats": get_author_stats, "list_lore": list_lore,
    }.items():
        monkeypatch.setattr(web_app.repo, name, fn)
    monkeypatch.setattr(membership, "is_member", is_member)
    return state


def tab_labels(html: str) -> list[str]:
    bar = html.split('class="tabbar"', 1)[1].split("</nav>", 1)[0]
    return [part.split("</span>", 1)[0] for part in bar.split("<span>")[1:]]


def test_member_gets_the_admin_like_tabbar(portal_world: dict[str, Any]):
    page = client_as(MEMBER).get("/g/1")
    assert page.status_code == 200
    assert tab_labels(page.text) == ["Обзор", "Выпуски", "Рейтинги", "Лор"]
    assert "--tabs: 4" in page.text
    # ни настроек, ни ссылки в админку
    assert 'href="/"' not in page.text and "Админка" not in page.text
    assert "Отбор" not in page.text and "Настройки" not in page.text


def test_digests_mode_hides_ratings_and_lore(portal_world: dict[str, Any]):
    portal_world["portal"] = "digests"
    client = client_as(MEMBER)
    page = client.get("/g/1")
    assert tab_labels(page.text) == ["Обзор", "Выпуски"]
    assert "Из лора" not in page.text
    assert client.get("/g/1/lore").status_code == 404
    assert client.get("/g/1/ratings").status_code == 404


def test_member_lore_has_no_roles(portal_world: dict[str, Any]):
    page = client_as(MEMBER).get("/g/1/lore")
    assert page.status_code == 200
    assert "Сделай красиво" in page.text
    assert "Главный скептик" not in page.text and "Петя" not in page.text


def test_overview_shows_latest_issue_headline(portal_world: dict[str, Any]):
    page = client_as(MEMBER).get("/g/1")
    assert "Veo подешевел — и чат &lt;b&gt;взорвался&lt;/b&gt;" in page.text
    assert "Главный скептик" not in page.text
    assert f'href="/g/1/d/{DAY.isoformat()}"' in page.text


def test_digests_archive(portal_world: dict[str, Any]):
    page = client_as(MEMBER).get("/g/1/digests")
    assert page.status_code == 200
    assert f"/g/1/d/{DAY.isoformat()}" in page.text


def test_issue_page_reads_like_an_article(portal_world: dict[str, Any]):
    page = client_as(MEMBER).get(f"/g/1/d/{DAY.isoformat()}")
    assert page.status_code == 200
    text = page.text
    assert 'class="issue-title"' in text and "12 рублей за ролик" in text
    assert "&lt;script&gt;x&lt;/script&gt;" in text and "<script>x" not in text
    assert text.count("<p>Абзац") == 2
    assert "Не по делу, но интересно" in text
    assert text.index("12 рублей за ролик") < text.index("Не по делу") < text.index("Питер ждёт")
    assert "<b>Вася</b> — за Veo" in text
    # новости — аккордеон: свёрнуты, открывается одна, якорь — для ссылок из поста
    assert '<details class="story" id="t11" name="issue">' in text
    assert "<details open" not in text
    # без оценок и признаков отбора
    assert "usefulness" not in text and "0.8" not in text


def test_issue_without_article_uses_the_same_layout(
    portal_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
):
    plain = Digest(id=5, chat_id=1, day=DAY, summary_md="",
                   payload=digest_data(article={}).to_dict(), msg_count=86)

    async def get_digest(chat_id: int, day: date) -> Digest | None:
        return plain

    monkeypatch.setattr(web_app.repo, "get_digest", get_digest)
    page = client_as(MEMBER).get(f"/g/1/d/{DAY.isoformat()}")
    assert page.status_code == 200
    # те же темы в той же раскладке, чтобы ссылки из поста работали
    assert "Дайджест за 24.09" in page.text
    assert '<details class="story" id="t11" name="issue">' in page.text
    assert "Цены на генерацию" in page.text and "Вася посчитал" in page.text
