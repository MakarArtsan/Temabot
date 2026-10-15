"""«Жизнь чата»: весь оффтоп дня — одной колонкой-расследованием (решение владельца, 15.10).

Раньше в раздел «не по делу» шли три лучшие темы, и вечерняя сага, разбитая
нарезкой на десяток кусков, в выпуск не попадала.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from src.db.models import Chat
from src.digest import pipeline as dp
from src.digest import prompts
from src.digest.publish import group_parts
from src.digest.render import (
    HASHTAG,
    DigestData,
    Topic,
    digest_messages,
    render,
    render_html,
    teaser_html,
)
from src.llm.client import Usage

DAY = date(2026, 10, 14)


def topic(n: int, title: str, kind: str, people: list[str], summary: str = "") -> Topic:
    return Topic(thread_id=n, title=title, kind=kind, participants=people,
                 summary=summary or f"{title}. Подробности.", msg_count=5 + n % 7)


SAGA = [
    topic(325829, "Алмаз выбирает имя сыну, Артёма вычеркнули", "fun", ["Алмаз", "Милана"]),
    topic(325926, "Клубное чадо: имя на «А» ищут всем чатом", "fun", ["Алмаз", "Дария", "Вера"]),
    topic(325949, "Аладдин Алмазович — будущий рэп-артист", "fun", ["Алмаз", "Полина"]),
    topic(325973, "Чат сватает Милану за Алмаза", "fun", ["Милана", "Алмаз", "Саша"]),
    topic(325836, "Дария уточняет у Саши, свободна ли она", "other", ["Дария", "Саша"]),
    topic(326356, "Полина выступает со стендапом", "life", ["Полина", "Ник"]),
    topic(326459, "Перепалка про заказчика за 60К", "drama", ["Дария", "Анастасия"]),
]
WORK = [topic(326287, "Как качать треки из Suno", "resource", ["Ник", "Дария"])]


class LifeLLM:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, messages: list[dict[str, str]], *, purpose: str, **kw: Any) -> Any:
        self.calls.append({"purpose": purpose, "system": messages[0]["content"],
                           "user": messages[1]["content"]})
        return self.answer, Usage(100, 50)


ANSWER = {
    "headline": "Расследование: кто сосватал Милану и как назовут сына Алмаза",
    "lead": "Редакции стало известно, что вечер в чате закончился помолвкой. Почти.",
    "episodes": [
        {"title": "Дело об имени на «А»", "text": "Следствие установило: Артёма вычеркнули.\n\n"
                                                    "Финалист — Аладдин Алмазович.",
         "thread_ids": [325829, 325926, 325949, 999999]},
        {"title": "Клуб одиночек открыт", "text": "Источники сообщают о сватовстве.",
         "thread_ids": [325973, 325836]},
        {"title": "", "text": "эпизод без заголовка выбрасываем", "thread_ids": []},
    ],
    "cast": ["Алмаз", "Милана", "Дария", "Вера", "Полина", "Саша", "Ник", "Анастасия"],
}


def chat() -> Chat:
    return Chat(id=1, tg_id=-100111, title="Нейросети")


# ================================================================ материал

def test_all_offtopic_goes_to_the_column_not_just_three():
    material = dp.life_material(SAGA + WORK)
    assert [t.thread_id for t in material] == sorted(t.thread_id for t in SAGA), \
        "вся сага по порядку, рабочее — не сюда"


def test_empty_flood_is_not_material():
    flood = topic(1, "привет", "other", ["Вася"])
    flood.summary = ""
    assert dp.life_material([flood]) == []


async def test_column_prompt_has_every_story_and_everyone():
    llm = LifeLLM(ANSWER)
    _, usage = await dp.make_life(SAGA + WORK, chat(), DAY, llm=llm)

    call = llm.calls[0]
    assert call["purpose"] == "life" and call["system"] == prompts.LIFE_SYSTEM
    for t in SAGA:
        assert f"{t.thread_id}. {t.title}" in call["user"]
    assert "Suno" not in call["user"], "рабочие темы — в первой части выпуска"
    assert "Участники этих разговоров: Алмаз, Анастасия, Вера, Дария, Милана, Ник, Полина, Саша" \
        in call["user"]
    assert "расследование" in prompts.LIFE_SYSTEM and "каждого участника" in prompts.LIFE_SYSTEM
    assert usage.tokens_in == 100


async def test_column_answer_is_checked():
    life, _ = await dp.make_life(SAGA, chat(), DAY, llm=LifeLLM(ANSWER))
    assert life["headline"].startswith("Расследование")
    assert [e["title"] for e in life["episodes"]] == [
        "Дело об имени на «А»", "Клуб одиночек открыт"]
    assert life["episodes"][0]["thread_ids"] == [325829, 325926, 325949], "выдуманный номер убран"


async def test_no_material_no_call():
    llm = LifeLLM(ANSWER)
    assert await dp.make_life(WORK, chat(), DAY, llm=llm) == ({}, Usage())
    assert llm.calls == []


async def test_model_says_nothing_lively_happened():
    life, _ = await dp.make_life(SAGA, chat(), DAY, llm=LifeLLM({"headline": ""}))
    assert life == {}


async def test_model_failure_leaves_the_issue_without_the_column():
    async def broken(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("модель легла")

    life, usage = await dp.make_life(SAGA, chat(), DAY, llm=broken)
    assert life == {} and usage.tokens_in == 0


# ================================================================== показ

def data(**kw: Any) -> DigestData:
    defaults: dict[str, Any] = dict(chat_tg_id=-100111, day=DAY, chat_title="Нейросети",
                                    highlights=["Suno качается без тарифа"], topics=WORK,
                                    msg_count=1163, participants=40)
    defaults.update(kw)
    return DigestData(**defaults)


async def life() -> dict[str, Any]:
    result, _ = await dp.make_life(SAGA, chat(), DAY, llm=LifeLLM(ANSWER))
    return result


async def test_column_survives_storage():
    column = await life()
    assert DigestData.from_dict(data(life=column).to_dict()).life == column


async def test_telegram_gets_two_parts():
    html = render_html(data(life=await life()))
    work_at = html.index("Как качать треки из Suno")
    life_at = html.index("☕ <b>Жизнь чата: Расследование")
    assert work_at < life_at, "сначала работа, потом жизнь чата"
    assert "🔎 <b>Дело об имени на «А»</b>\n<blockquote expandable>" in html
    assert "Не по делу, но интересно" not in html


async def test_long_column_fits_telegram_messages():
    column = await life()
    column["episodes"] = [{"title": f"Эпизод {i}", "text": "очень длинно. " * 300,
                           "thread_ids": []} for i in range(5)]
    for part in digest_messages(data(life=column)):
        assert len(part) <= 4096


async def test_markdown_archive_has_the_column():
    md = render(data(life=await life()))
    assert "Жизнь чата" in md and "Клуб одиночек открыт" in md


async def test_quiet_work_day_with_lively_chat_is_not_empty():
    """Работы не было, а чат жил — выпуск всё равно есть."""
    from src.digest.publish import is_empty

    assert not is_empty(data(topics=[], highlights=[], life=await life()))


async def test_short_post_links_to_the_column():
    url = "https://site.example.com/g/1/d/2026-10-14"
    post = teaser_html(data(life=await life()), url, number=9)
    assert f'☕ <a href="{url}#life">Жизнь чата: Расследование' in post
    assert post.rstrip().endswith(HASHTAG)
    assert group_parts(data(life=await life()), ratings_public=False, url=url, number=9) == [post]


async def test_column_on_the_site(monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient
    from src.config import cfg
    from src.db.models import Digest
    from src.web import app as web_app
    from src.web import auth, membership

    owner, member = 132036441, 555001
    for target in {id(cfg): cfg, id(auth.cfg): auth.cfg, id(web_app.cfg): web_app.cfg}.values():
        monkeypatch.setattr(target, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
        monkeypatch.setattr(target, "OWNER_ID", owner)
        monkeypatch.setattr(target, "DATABASE_URL", "")
    group = Chat(id=1, tg_id=-100111, title="Нейросети", portal="all")
    digest = Digest(id=5, chat_id=1, day=DAY, summary_md="",
                    payload=data(life=await life()).to_dict(), msg_count=1163)

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return group

    async def list_chat_digests(chat_id: int, limit: int = 60) -> list[Digest]:
        return [digest]

    async def get_digest(chat_id: int, day: date) -> Digest:
        return digest

    async def is_member(chat: Chat, user_id: int, **kw: Any) -> bool:
        return True

    for name, fn in {"get_chat_by_id": get_chat_by_id, "list_chat_digests": list_chat_digests,
                     "get_digest": get_digest}.items():
        monkeypatch.setattr(web_app.repo, name, fn)
    monkeypatch.setattr(membership, "is_member", is_member)
    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    client.cookies.set(auth.COOKIE_NAME, auth.issue_session(member))

    page = client.get(f"/g/1/d/{DAY.isoformat()}").text
    assert '<section class="life" id="life">' in page
    assert "Клуб одиночек открыт" in page and "Следствие установило" in page
    assert page.index("Как качать треки из Suno") < page.index('id="life"')
