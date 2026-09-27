"""Живой дайджест: раскрывающиеся темы, кто спорил, оффтоп отдельно, лор чата."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from src.db import repo
from src.db.models import Chat, Message
from src.digest import lore as lore_mod
from src.digest import pipeline as dp
from src.digest import prompts
from src.digest.publish import group_parts
from src.digest.render import (
    TELEGRAM_LIMIT,
    DigestData,
    Topic,
    digest_messages,
    render_html,
    topic_card,
)
from src.llm.client import Usage
from src.nlp.threads import segment
from src.scoring import score as scoring
from src.scoring.llm_rubric import Rubric

CHAT_TG_ID = -1002354231333
DAY = date(2026, 9, 26)
VASYA, PETYA, MASHA = 101, 102, 103


def chat(**kw: Any) -> Chat:
    return Chat(id=1, tg_id=CHAT_TG_ID, title="Нейросети", **kw)


def work_topic(n: int = 1, **kw: Any) -> Topic:
    defaults: dict[str, Any] = dict(
        thread_id=n, title=f"Сколько стоит ролик {n}", kind="decision",
        summary="Вася посчитал, что Veo выходит в 12 ₽ за ролик, Петя возразил про Kling.",
        short="Veo — 12 ₽ за ролик",
        sides=[{"who": "Вася", "stance": "за Veo"}, {"who": "Петя", "stance": "за Kling"}],
        decision="для клиентов берём Veo", why="считать бюджет",
        participants=["Вася", "Петя"], msg_count=12, key_msg_ids=[40 + n],
    )
    defaults.update(kw)
    return Topic(**defaults)


def life_topic() -> Topic:
    return Topic(thread_id=90, title="Маша переезжает в Питер", kind="life",
                 summary="Маша рассказала, что с октября работает в Питере; все поздравляли.",
                 participants=["Маша", "Оля"], msg_count=9)


def data(topics: list[Topic], **kw: Any) -> DigestData:
    return DigestData(chat_tg_id=CHAT_TG_ID, day=DAY, chat_title="Нейросети",
                      highlights=["Veo дешевле, чем думали"], topics=topics,
                      msg_count=120, participants=9, **kw)


def balanced(html: str) -> bool:
    return html.count("<blockquote expandable>") == html.count("</blockquote>") and (
        html.count("<b>") == html.count("</b>")
    )


# ================================================================= формат

def test_each_topic_is_a_title_with_details_folded():
    html = render_html(data([work_topic()]))

    assert "🟢 <b>Сколько стоит ролик 1</b>\n<blockquote expandable>" in html
    body = html.split("<blockquote expandable>", 1)[1].split("</blockquote>")[0]
    assert "Вася посчитал" in body, "пересказ с именами — внутри раскрывающегося блока"
    assert "🗣 Спорили: Вася — за Veo; Петя — за Kling" in body
    assert "✅ Итог: для клиентов берём Veo" in body
    assert "к обсуждению" in body


def test_decision_is_not_repeated_when_summary_already_says_it():
    topic = work_topic(
        summary="Сошлись, что для клиентов берём Veo", decision="для клиентов берём Veo"
    )
    assert "✅ Итог" not in render_html(data([topic]))


def test_offtopic_goes_to_its_own_section_after_work():
    html = render_html(data([life_topic(), work_topic()]))

    section = html.index("Не по делу, но интересно")
    assert html.index("Сколько стоит ролик") < section < html.index("Маша переезжает")


def test_names_and_texts_from_the_model_are_escaped():
    topic = work_topic(
        title="<b>взлом</b>", sides=[{"who": "<i>Вася</i>", "stance": "за <s>Veo</s>"}]
    )
    html = render_html(data([topic]))

    assert "<b>взлом</b>" not in html and "&lt;b&gt;взлом&lt;/b&gt;" in html
    assert "&lt;i&gt;Вася&lt;/i&gt; — за &lt;s&gt;Veo&lt;/s&gt;" in html


def test_long_digest_is_split_by_topics_not_mid_tag():
    long = "очень подробно. " * 60
    topics = [work_topic(n, summary=long) for n in range(1, 16)]
    parts = digest_messages(data(topics, heroes="🏅 Герои дня: Вася"))

    assert len(parts) > 1, "не влезло в одно — стало несколько сообщений"
    assert all(len(p) <= TELEGRAM_LIMIT for p in parts)
    assert all(balanced(p) for p in parts), "разметка не разорвана"
    joined = "\n\n".join(parts)
    order = [joined.index(f"Сколько стоит ролик {n}<") for n in range(1, 16)]
    assert order == sorted(order), "темы идут по порядку"


def test_one_enormous_topic_is_trimmed_not_broken():
    topic = work_topic(summary="слово " * 3000)
    parts = digest_messages(data([topic]))

    assert all(len(p) <= TELEGRAM_LIMIT and balanced(p) for p in parts)


def test_short_day_fits_one_message():
    assert len(digest_messages(data([work_topic(), life_topic()]))) == 1


def test_old_stored_digest_still_renders():
    """Дайджесты, собранные до переделки, — без пересказа и сторон."""
    old = DigestData.from_dict({
        "chat_tg_id": CHAT_TG_ID, "day": "2026-09-20",
        "topics": [{"thread_id": 1, "title": "Старое", "kind": "insight",
                    "takeaway": "вывод", "debate": "спорили о цене", "msg_count": 3}],
    })
    html = render_html(old)
    assert "вывод" in html and "Спорили: спорили о цене" in html and balanced(html)


def test_topic_card_for_rating_is_short():
    card = topic_card(work_topic(), CHAT_TG_ID)

    assert "Сколько стоит ролик 1" in card and "Veo — 12 ₽ за ролик" in card
    assert "blockquote" not in card and "к обсуждению" in card


def test_group_version_is_the_same_single_message():
    parts = group_parts(data([work_topic()], heroes="🏅 Герои дня: Вася"), ratings_public=False)
    assert len(parts) == 1 and "<blockquote expandable>" in parts[0]
    assert "Герои дня" not in parts[0]


# ============================================================ отправка владельцу

async def test_owner_gets_whole_digest_then_quiet_cards_to_rate():
    from src.digest.scheduler import send_digest

    sent: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, chat_id: int, text: str, **kw: Any) -> None:
            sent.append({"text": text, **kw})

    topics = [work_topic(1, item_id=11), life_topic()]
    topics[1].item_id = 12
    count = await send_digest(Bot(), 5, data(topics), topics=topics)

    assert count == 3
    assert "<blockquote expandable>" in sent[0]["text"], "сначала весь дайджест"
    assert sent[0].get("reply_markup") is None
    cards = sent[1:]
    assert all(c["reply_markup"] is not None and c["disable_notification"] for c in cards)
    assert "Маша переезжает" in cards[1]["text"]


# ============================================================== разбор треда

def fake_llm(payload: Any):
    calls: list[dict[str, Any]] = []

    async def call(messages, *, purpose: str, chat_id: int | None = None, **kw: Any):
        calls.append({"messages": messages, "purpose": purpose})
        return payload, Usage(10, 5, "test")

    call.calls = calls  # type: ignore[attr-defined]
    return call


def a_thread() -> Any:
    start = datetime(2026, 9, 26, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    return segment([
        Message(chat_id=1, tg_msg_id=1, tg_user_id=VASYA, author_name="Вася",
                text="Veo или Kling?", date=start),
        Message(chat_id=1, tg_msg_id=2, tg_user_id=PETYA, author_name="Петя", reply_to=1,
                text="Kling дешевле", date=start),
        Message(chat_id=1, tg_msg_id=3, tg_user_id=VASYA, author_name="Вася", reply_to=2,
                text="на коротких Veo", date=start),
    ])[0]


async def test_map_reads_summary_and_who_argued():
    llm = fake_llm({
        "title": "Veo или Kling", "summary": "Вася за Veo, Петя за Kling.", "short": "спор о цене",
        "sides": [{"who": "Вася", "stance": "Veo"}, {"who": "", "stance": "пусто"}, "мусор",
                  {"who": "Петя", "stance": "Kling"}],
    })
    topic, _ = await dp.map_thread(a_thread(), chat(), llm=llm)

    assert topic is not None
    assert topic.summary == "Вася за Veo, Петя за Kling." and topic.short == "спор о цене"
    assert topic.sides == [{"who": "Вася", "stance": "Veo"}, {"who": "Петя", "stance": "Kling"}]


async def test_chat_lore_goes_into_the_prompt():
    llm = fake_llm({"title": "тема"})
    await dp.map_thread(a_thread(), chat(), llm=llm, lore="- Семёныч: легендарный прораб")

    system = llm.calls[0]["messages"][0]["content"]
    assert "Семёныч: легендарный прораб" in system
    assert "Называй людей по именам" in system


def test_prompts_ask_for_human_language_and_offtopic():
    assert "без канцелярита" in prompts.MAP_SYSTEM
    assert "личная новость" in prompts.MAP_SYSTEM and 'title = ""' in prompts.MAP_SYSTEM
    assert "life" in prompts.RUBRIC_SYSTEM and "interest" in prompts.RUBRIC_SYSTEM


# ======================================================== отбор оффтопа

def scored(kind: str, *, usefulness: float, interest: float, engagement: float = 0.8):
    rubric = Rubric(kind=kind, usefulness=usefulness, interest=interest, specificity=3,
                    relevance=4)
    return scoring.compute(engagement=engagement, rubric=rubric, novelty=1.0, normalized={})


def test_lively_personal_news_passes_even_without_usefulness():
    news = scored("life", usefulness=0, interest=8)
    flood = scored("other", usefulness=0, interest=8)

    assert news.passed, "личная новость, которую живо обсуждали, — в дайджест"
    assert not flood.passed, "интерес не спасает то, что модель признала флудом"
    assert news.features["interest"] == 0.8


def test_dull_joke_does_not_pass():
    assert not scored("fun", usefulness=0, interest=1).passed


def test_offtopic_has_its_own_limit_and_does_not_crowd_out_work():
    work = [(f"w{i}", scored("insight", usefulness=8, interest=0)) for i in range(3)]
    fun = [(f"f{i}", scored("fun", usefulness=0, interest=10)) for i in range(4)]

    shown, missed = scoring.select(work + fun, settings={"top_n": 3, "top_n_offtopic": 2})

    assert sorted(shown) == ["f0", "f1", "w0", "w1", "w2"]
    assert sorted(missed) == ["f2", "f3"]
    shown, _ = scoring.select(work + fun, settings={"top_n": 3, "top_n_offtopic": 0})
    assert sorted(shown) == ["w0", "w1", "w2"], "0 — раздел выключен"


# ================================================================== лор

class LoreLLM:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[Any] = []

    async def __call__(self, messages, *, purpose: str, chat_id: int | None = None, **kw: Any):
        self.calls.append(messages)
        return self.answer, Usage(10, 5, "test")


@pytest.fixture
def lore_repo(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"added": [], "touched": [], "existing": [], "opted_out": set()}

    async def list_lore(chat_id: int, *, include_hidden: bool = False) -> list[dict]:
        rows = state["existing"]
        return rows if include_hidden else [r for r in rows if not r.get("hidden")]

    async def add_lore(chat_id: int, **kw: Any) -> int:
        state["added"].append(kw)
        return len(state["added"])

    async def touch_lore(lore_id: int, chat_id: int, **kw: Any) -> bool:
        state["touched"].append((lore_id, kw))
        return True

    async def opted_out_ids() -> set[int]:
        return state["opted_out"]

    monkeypatch.setattr(repo, "list_lore", list_lore)
    monkeypatch.setattr(repo, "add_lore", add_lore)
    monkeypatch.setattr(repo, "touch_lore", touch_lore)
    monkeypatch.setattr(repo, "opted_out_ids", opted_out_ids)
    return state


async def test_lore_answer_is_checked(lore_repo: dict[str, Any]):
    lore_repo["existing"] = [{"id": 7, "kind": "meme", "title": "как у Семёныча", "body": ""}]
    lore_repo["opted_out"] = {MASHA}
    llm = LoreLLM({
        "add": [
            {"kind": "meme", "title": "Кот-бухгалтер", "body": "кот, который считает бюджет",
             "who": None, "msg_ids": [41, 999]},
            {"kind": "role", "title": "голос разума", "body": "всех мирит", "who": VASYA},
            {"kind": "role", "title": "шутник", "body": "", "who": MASHA},     # скрылась
            {"kind": "role", "title": "призрак", "body": "", "who": 555},      # не участник
            {"kind": "гадость", "title": "что-то", "body": ""},
            {"kind": "story", "title": "", "body": "без названия"},
        ],
        "update": [{"id": 7, "body": "теперь и про ремонт"}, {"id": 999, "body": "чужое"}],
    })

    added = await lore_mod.update_lore(
        chat(), DAY, [work_topic(1)], people={VASYA: "Вася", MASHA: "Маша"}, llm=llm,
    )

    kinds = [(a["kind"], a["title"]) for a in lore_repo["added"]]
    assert kinds == [("meme", "Кот-бухгалтер"), ("role", "Вася — голос разума")]
    assert lore_repo["added"][0]["sources"] == [{"day": "2026-09-26", "msg_id": 41}]
    assert lore_repo["added"][1]["tg_user_id"] == VASYA
    assert lore_repo["touched"] == [(7, {"day": DAY, "body": "теперь и про ремонт"})]
    assert added == ["😄 мем «Кот-бухгалтер»", "🎭 роль «Вася — голос разума»"]
    prompt = llm.calls[0][1]["content"]
    assert "Маша" not in prompt.split("Участники дня")[1].split("Обсуждения дня")[0]


async def test_lore_can_be_switched_off(lore_repo: dict[str, Any]):
    llm = LoreLLM({"add": [{"kind": "meme", "title": "x", "body": ""}]})
    off = chat(settings={"lore": {"enabled": False}})

    assert await lore_mod.update_lore(off, DAY, [work_topic()], llm=llm) == []
    assert llm.calls == [] and await lore_mod.context_lines(off) == ""


async def test_lore_context_skips_roles_and_hidden(lore_repo: dict[str, Any]):
    lore_repo["existing"] = [
        {"id": 1, "kind": "legend", "title": "Семёныч", "body": "легендарный прораб"},
        {"id": 2, "kind": "role", "title": "Вася — голос разума", "body": "мирит"},
        {"id": 3, "kind": "meme", "title": "скрытое", "body": "", "hidden": True},
    ]
    assert await lore_mod.context_lines(chat()) == "- Семёныч: легендарный прораб"


@pytest.mark.usefixtures("db")
async def test_lore_in_the_real_database():
    stored = await repo.bootstrap_primary_chat(CHAT_TG_ID)
    first = await repo.add_lore(stored.id, kind="meme", title="Кот-бухгалтер", body="считает",
                                day=date(2026, 9, 25))
    again = await repo.add_lore(stored.id, kind="meme", title="кот-бухгалтер", body="другое",
                                day=DAY)

    assert first is not None and again is None, "то же название — та же запись"
    rows = await repo.list_lore(stored.id)
    assert len(rows) == 1 and rows[0]["mentions"] == 2 and rows[0]["last_day"] == DAY
    assert rows[0]["body"] == "считает", "описание само не затирается"

    await repo.touch_lore(first, stored.id, day=DAY, body="новое описание")
    await repo.set_lore_hidden(first, True)
    assert await repo.list_lore(stored.id) == []
    assert (await repo.list_lore(stored.id, include_hidden=True))[0]["body"] == "новое описание"
    assert await repo.delete_lore(first) is True


# ============================================================ админка

def test_lore_page_is_owner_only(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient
    from src.web import app as web_app
    from src.web import auth

    # тот самый cfg, что у админки: test_config умеет перезагружать модуль настроек
    for settings in {id(auth.cfg): auth.cfg, id(web_app.cfg): web_app.cfg}.values():
        monkeypatch.setattr(settings, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
        monkeypatch.setattr(settings, "OWNER_ID", 132036441)

    async def list_chats(**kw: Any) -> list[Chat]:
        return [chat()]

    async def list_lore(chat_id: int, *, include_hidden: bool = False) -> list[dict]:
        return [{"id": 1, "kind": "legend", "title": "Семёныч", "body": "прораб", "mentions": 3,
                 "hidden": False, "first_day": DAY, "last_day": DAY, "sources": []}]

    monkeypatch.setattr(web_app.repo, "list_chats", list_chats)
    monkeypatch.setattr(web_app.repo, "list_lore", list_lore)

    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    assert client.get("/lore").status_code == 303

    client.cookies.set(auth.COOKIE_NAME, auth.issue_session(555001))
    assert client.get("/lore").status_code == 303, "участнику лор не показываем"

    client.cookies.set(auth.COOKIE_NAME, auth.issue_session(132036441))
    page = client.get("/lore")
    assert page.status_code == 200 and "Семёныч" in page.text
    assert "Легендарные персонажи" in page.text
    assert re.search(r"всплывало 3 дня", page.text)
