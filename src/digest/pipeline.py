"""Дайджест: map-reduce по тредам за сутки (TZ §4.3).

Отбор тем здесь простой — по вовлечённости. На шаге 11 он заменяется скорингом
из §4.7; ради этого reduce вынесен в отдельную функцию `rank_topics`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from typing import Any

from src.config import cfg
from src.db import pool, repo
from src.db.models import Chat, Message
from src.digest import prompts
from src.digest.render import DigestData, Topic, render, render_html
from src.llm.client import Usage, chat_json
from src.nlp.threads import Thread, segment
from src.scoring import score as scoring
from src.scoring.llm_rubric import Rubric, rate_thread
from src.scoring.novelty import score_novelty
from src.scoring.signals import ThreadSignals, collect_signals, engagement, normalize

log = logging.getLogger(__name__)

DEFAULT_TOP_N = 6
MAX_THREAD_CHARS = 12_000   # длинный тред режем, чтобы не разориться на токенах

# Шум по §4.3 п.2: реакции-репликами. Учитываются в статистике, но не в map-стадии.
NOISE_WORDS = {
    "+", "++", "ок", "окей", "ага", "угу", "да", "нет", "спасибо", "спс", "пасиб",
    "круто", "класс", "супер", "топ", "огонь", "жиза", "понял", "принял", "ясно",
    "хорошо", "отлично", "здорово", "ну да", "вот вот", "плюсую", "согласен",
}
_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)

LLMCall = Callable[..., Awaitable[tuple[Any, Usage]]]


def is_noise(message: Message) -> bool:
    """Стикеры и короткие поддакивания — не содержание дня."""
    if message.media_type == "sticker":
        return True
    content = (message.content or "").strip()
    if not content:
        # медиа без текста — это не шум, его ещё расшифруют
        return message.media_type is None
    lowered = content.lower().strip()
    if lowered in NOISE_WORDS:          # «+», «++» — до вырезания пунктуации
        return True

    words = _WORD_RE.sub(" ", lowered).split()
    if not words:
        # только эмодзи или знаки препинания — это реакция, а не реплика
        return True
    if len(words) > 2:
        return False
    return " ".join(words) in NOISE_WORDS


def split_noise(messages: list[Message]) -> tuple[list[Message], list[Message]]:
    """Вернуть (содержательные, шум)."""
    meaningful: list[Message] = []
    noise: list[Message] = []
    for message in messages:
        (noise if is_noise(message) else meaningful).append(message)
    return meaningful, noise


@dataclass(slots=True)
class DigestResult:
    chat_tg_id: int
    day: date_type
    markdown: str
    topics: list[Topic]
    usage: Usage
    msg_count: int
    data: DigestData | None = None
    all_topics: list[Topic] = field(default_factory=list)  # включая отсеянные (/missed)
    mapped: int = 0            # тредов разобрано моделью
    failed: int = 0            # тредов, на которых модель не ответила
    digest_id: int | None = None

    @property
    def html(self) -> str:
        """Тот же дайджест в формате, который принимает Telegram."""
        return render_html(self.data) if self.data else self.markdown

    @property
    def llm_is_down(self) -> bool:
        """Все вызовы упали: сохранять пустой дайджест за день нельзя."""
        return self.failed > 0 and self.mapped == 0


# ------------------------------------------------------------------- map

async def map_thread(
    thread: Thread, chat: Chat, *, llm: LLMCall = chat_json
) -> tuple[Topic | None, Usage]:
    """Один тред -> одна тема. Ошибка модели не роняет весь дайджест."""
    participants = ", ".join(
        f"{m.tg_user_id} — {m.author_name or '?'}"
        for m in {m.tg_user_id: m for m in thread.messages if m.tg_user_id}.values()
    )
    system = prompts.MAP_SYSTEM
    profile = (chat.settings or {}).get("interests_profile")
    if profile:
        system += prompts.INTERESTS_HINT.format(profile=profile)

    try:
        data, usage = await llm(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": prompts.MAP_USER.format(
                        participants=participants or "неизвестны",
                        text=thread.text[:MAX_THREAD_CHARS],
                    ),
                },
            ],
            purpose="summary",
            chat_id=chat.id,
        )
    except Exception as exc:
        log.error("Не удалось разобрать тред %s: %s", thread.root_msg_id, exc)
        raise

    if not isinstance(data, dict) or not (data.get("title") or "").strip():
        # модель сама признала тред пустым
        return None, usage

    topic = Topic(
        thread_id=thread.root_msg_id,
        title=str(data.get("title", "")).strip(),
        decision=str(data.get("decision") or "").strip(),
        debate=str(data.get("debate") or "").strip(),
        open_questions=_as_str_list(data.get("open_questions")),
        links=_as_str_list(data.get("links")),
        mentions=_as_str_list(data.get("mentions")),
        key_msg_ids=_as_int_list(data.get("key_msg_ids"), allowed=set(thread.msg_ids)),
        contributors=_as_contributors(data.get("contributors"), thread),
        participants=[m.author_name or str(m.tg_user_id) for m in thread.messages],
        msg_count=len(thread.messages),
        reactions=sum(m.reactions for m in thread.messages),
    )
    topic.participants = sorted(set(topic.participants))
    return topic, usage


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _as_int_list(value: Any, *, allowed: set[int] | None = None) -> list[int]:
    """Модель любит выдумывать номера сообщений — оставляем только настоящие."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if allowed is None or number in allowed:
            result.append(number)
    return result


def _as_contributors(value: Any, thread: Thread) -> list[dict]:
    """Оставляем только тех, кто действительно писал в треде (§4.10)."""
    if not isinstance(value, list):
        return []
    known = thread.participants
    roles = {"initiator", "key", "answerer"}
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            user_id = int(item.get("tg_user_id", ""))
        except (TypeError, ValueError):
            continue
        role = str(item.get("role", "")).strip()
        if user_id in known and role in roles:
            result.append({"tg_user_id": user_id, "role": role})
    return result


# ---------------------------------------------------------------- reduce (§4.7)

async def score_topic(
    topic: Topic,
    thread: Thread,
    chat: Chat,
    *,
    signals: ThreadSignals,
    peers: list[ThreadSignals],
    history: dict[str, list[float]],
    recent_embeddings: list[list[float]],
    examples: list[dict[str, Any]],
    llm: LLMCall = chat_json,
) -> tuple[scoring.Scored, Usage]:
    """Три слоя отбора: сигналы, рубрика модели, новизна (TZ §4.7)."""
    normalized = normalize(signals, history, peers=peers)
    engagement_value = engagement(normalized)

    try:
        rubric, usage = await rate_thread(thread, chat, examples=examples, llm=llm)
    except Exception:
        # без рубрики тема не выбывает: остаются сигналы и новизна
        log.warning("Рубрика недоступна для треда %s", thread.root_msg_id)
        rubric, usage = Rubric(), Usage()

    novelty, embedding, similarity = await score_novelty(
        topic.title, rubric.takeaway, recent_embeddings
    )

    result = scoring.compute(
        engagement=engagement_value,
        rubric=rubric,
        novelty=novelty,
        normalized=normalized,
        settings=chat.settings,
        similarity=similarity,
    )
    result.features["raw_signals"] = signals.as_dict()
    result.features["takeaway"] = rubric.takeaway
    result.features["why"] = rubric.why

    topic.score = result.score
    topic.kind = rubric.kind
    topic.takeaway = rubric.takeaway
    topic.why = rubric.why
    topic.features = result.features
    topic.embedding = embedding
    topic.shown = result.passed
    return result, usage


async def make_highlights(
    topics: list[Topic], chat: Chat, *, llm: LLMCall = chat_json
) -> tuple[list[str], Usage]:
    """«Главное за день» — 3 строки поверх отобранных тем."""
    if not topics:
        return [], Usage()
    listing = "\n".join(
        f"- {t.title}" + (f" — {t.decision}" if t.decision else "") for t in topics
    )
    try:
        data, usage = await llm(
            [
                {"role": "system", "content": prompts.REDUCE_SYSTEM},
                {"role": "user", "content": prompts.REDUCE_USER.format(topics=listing)},
            ],
            purpose="summary",
            chat_id=chat.id,
        )
    except Exception:
        log.exception("Не удалось собрать «Главное за день»")
        return [], Usage()
    highlights = _as_str_list(data.get("highlights")) if isinstance(data, dict) else []
    return highlights[:3], usage


# -------------------------------------------------------------- сборка дня

def collect_unanswered(threads: list[Thread], topics: list[Topic]) -> list[tuple[str, int]]:
    """Вопросы без ответа (§4.3 п.5)."""
    by_thread = {t.thread_id: t for t in topics}
    result: list[tuple[str, int]] = []
    for thread in threads:
        topic = by_thread.get(thread.root_msg_id)
        if topic is None:
            continue
        for question in topic.open_questions:
            result.append((question, topic.anchor_msg_id))
    return result


async def build_digest(
    chat: Chat,
    day: date_type,
    *,
    llm: LLMCall = chat_json,
    top_n: int | None = None,
    now: datetime | None = None,
) -> DigestResult:
    """Собрать дайджест за день. В БД ничего не пишет — этим занимается run()."""
    messages = await repo.get_messages_by_day(chat.id, day)
    meaningful, noise = split_noise(messages)
    threads = segment(meaningful)

    # всё, что нужно скорингу, читаем один раз на весь день
    authors = await repo.list_author_weights()
    history = await repo.signal_history(chat.id, before=day)
    recent_embeddings = await repo.recent_topic_embeddings(chat.id, before=day)
    examples = await repo.feedback_examples(chat.id)

    live = [t for t in threads if not t.low_value]
    peers = [
        collect_signals(
            t, owner_id=cfg.OWNER_ID,
            author_weights=authors["weights"], muted_authors=authors["muted"],
        )
        for t in live
    ]

    total_usage = Usage()
    pairs: list[tuple[Topic, scoring.Scored]] = []
    mapped = failed = 0

    for thread, signals in zip(live, peers, strict=True):
        try:
            topic, usage = await map_thread(thread, chat, llm=llm)
        except Exception:
            # один упавший тред не отменяет остальные, но мы это запомним
            failed += 1
            continue
        mapped += 1
        total_usage = total_usage + usage
        if topic is None:
            continue

        result, usage = await score_topic(
            topic, thread, chat,
            signals=signals, peers=peers, history=history,
            recent_embeddings=recent_embeddings, examples=examples, llm=llm,
        )
        total_usage = total_usage + usage
        pairs.append((topic, result))

    settings = dict(chat.settings or {})
    if top_n is not None:
        settings["top_n"] = top_n
    selected, missed = scoring.select(pairs, settings=settings)
    for topic in missed:
        topic.shown = False

    highlights, usage = await make_highlights(selected, chat, llm=llm)
    total_usage = total_usage + usage

    links: list[str] = []
    for topic in selected:
        for link in topic.links:
            if link not in links:
                links.append(link)

    data = DigestData(
        chat_tg_id=chat.tg_id,
        day=day,
        chat_title=chat.title or "",
        highlights=highlights,
        topics=selected,
        unanswered=collect_unanswered(threads, selected),
        links=links,
        msg_count=len(messages),
        participants=len({m.tg_user_id for m in messages if m.tg_user_id}),
        noise_count=len(noise),
        busiest_thread=max(selected, key=lambda t: t.msg_count, default=None),
        low_value_count=sum(1 for t in threads if t.low_value),
    )

    return DigestResult(
        chat_tg_id=chat.tg_id,
        day=day,
        markdown=render(data),
        topics=selected,
        usage=total_usage,
        msg_count=len(messages),
        data=data,
        all_topics=[t for t, _ in pairs],
        mapped=mapped,
        failed=failed,
    )


async def run_for_chat(
    chat: Chat, day: date_type, *, save: bool = True, llm: LLMCall = chat_json
) -> DigestResult:
    result = await build_digest(chat, day, llm=llm)
    if result.llm_is_down:
        # иначе за днём навсегда закрепится пустой дайджест из-за сбоя модели
        raise RuntimeError(
            f"Модель не ответила ни на один из {result.failed} тредов — "
            f"дайджест за {day} не сохранён"
        )
    if save:
        # повторный прогон за тот же день перезаписывает запись (§4.3 п.8)
        result.digest_id = await repo.save_digest(
            chat.id,
            day,
            result.markdown,
            payload=result.data.to_dict() if result.data else {},
            msg_count=result.msg_count,
            tokens_used=result.usage.tokens_in + result.usage.tokens_out,
        )
        # отсеянные темы тоже сохраняем: они нужны для /missed и для обучения
        item_ids = await repo.save_digest_items(
            result.digest_id,
            chat.id,
            [_topic_as_item(t) for t in result.all_topics],
        )
        for topic, item_id in zip(result.all_topics, item_ids, strict=False):
            topic.item_id = item_id

        # вклад участников в треды — основа рейтингов (TZ §4.10)
        await repo.save_thread_contributions(
            chat.id,
            day,
            [
                {"thread_id": topic.thread_id, "tg_user_id": c["tg_user_id"],
                 "role": c["role"]}
                for topic in result.all_topics
                for c in topic.contributors
            ],
        )
        await _add_heroes(chat, day, result)
    return result


async def _add_heroes(chat: Chat, day: date_type, result: DigestResult) -> None:
    """Дописать строку «Герои дня» (TZ §4.10).

    Статистику считаем прямо здесь: job рейтингов идёт в 23:40, после дайджеста,
    а герои нужны уже сейчас. Пересчёт идемпотентный, так что job ничего не сломает.
    """
    if result.data is None:
        return
    try:
        from src.bot.handlers_ratings import heroes_line
        from src.jobs.ratings import recalc_day

        await recalc_day(chat, day)
        rows = await repo.get_author_stats(chat.id, date_from=day, date_to=day)
        line = heroes_line(rows)
    except Exception:
        # без героев дайджест остаётся дайджестом
        log.warning("Не удалось посчитать героев дня", exc_info=True)
        return

    if not line:
        return
    result.data.heroes = line
    result.markdown = render(result.data)


def _topic_as_item(topic: Topic) -> dict[str, Any]:
    from src.nlp.embed import to_pgvector

    return {
        "thread_id": topic.thread_id,
        "title": topic.title,
        "kind": topic.kind,
        "features": topic.features,
        "score": topic.score,
        "shown": topic.shown,
        "embedding": to_pgvector(topic.embedding) if topic.embedding else None,
    }


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Собрать дайджест за день")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--chat", type=int, default=None, help="tg_id группы")
    parser.add_argument("--no-save", action="store_true", help="не писать в БД")
    args = parser.parse_args()

    logging.basicConfig(
        level=cfg.LOG_LEVEL, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    day = date_type.fromisoformat(args.date)

    try:
        chats = await repo.list_chats(digest=True) if args.chat is None else []
        if args.chat is not None:
            found = await repo.get_chat_by_tg_id(args.chat)
            chats = [found] if found else []
        if not chats:
            raise SystemExit(
                "Нет чатов с digest = true. Укажи --chat <tg_id> или включи группу."
            )
        for chat in chats:
            result = await run_for_chat(chat, day, save=not args.no_save)
            print("\n" + "=" * 60)
            print(result.markdown)
            print("=" * 60)
            print(
                f"токенов: {result.usage.tokens_in} на вход, "
                f"{result.usage.tokens_out} на выход; сообщений за день: {result.msg_count}"
            )
    finally:
        await pool.close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
