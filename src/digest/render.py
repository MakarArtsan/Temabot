"""Рендер дайджеста (TZ §4.3, пункты 5-6).

Два формата из одних данных:
  * Markdown — для хранения и веб-админки;
  * Telegram HTML — для отправки. HTML выбран потому, что в нём экранируется
    ровно три символа, а Markdown Telegram ломается на любой звёздочке или
    подчёркивании внутри текста, пришедшего от модели.

В Telegram дайджест — одно сообщение: у каждой темы виден заголовок, а
подробности свёрнуты в раскрывающуюся цитату (`<blockquote expandable>`).
Если текст не влезает в лимит Telegram, он делится на несколько сообщений —
только по границам тем, чтобы не разорвать разметку.

Структура дайджеста целиком складывается в `digests.topics`, чтобы команда
`/digest <дата>` за прошедший день собирала ровно ту же картинку, а не
пыталась превращать хранимый текст обратно в разметку.
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date as date_type
from typing import Any

SUPERGROUP_PREFIX = 1_000_000_000_000
TELEGRAM_LIMIT = 4096

_MD_SPECIAL = re.compile(r"([*_`\[\]])")


def deeplink(chat_tg_id: int, tg_msg_id: int) -> str:
    """Ссылка на сообщение супергруппы (TZ §4.3 п.6).

    У супергрупп id вида -100XXXXXXXXXX, а в ссылке нужен номер без приставки.
    """
    internal = abs(chat_tg_id) - SUPERGROUP_PREFIX
    return f"https://t.me/c/{internal}/{tg_msg_id}"


def esc_md(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text or "")


def esc_html(text: str) -> str:
    return html_lib.escape(text or "", quote=False)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Разбить длинный текст на части по границам абзацев.

    Telegram не принимает сообщения длиннее 4096 символов, а дайджест активного
    дня легко их перебирает.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        parts.append(current)
    return parts


@dataclass(slots=True)
class Topic:
    """Тема дайджеста — результат map-стадии плюс подсчитанные сигналы."""

    thread_id: int
    title: str
    summary: str = ""            # живой пересказ с именами: кто что предложил и чем кончилось
    short: str = ""              # суть одной строкой
    sides: list[dict] = field(default_factory=list)   # [{"who": имя, "stance": позиция}]
    decision: str = ""
    debate: str = ""             # старые дайджесты: спор одной строкой вместо sides
    open_questions: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    key_msg_ids: list[int] = field(default_factory=list)
    contributors: list[dict] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    msg_count: int = 0
    reactions: int = 0
    score: float = 0.0
    # заполняется скорингом (TZ §4.7)
    kind: str = "other"
    takeaway: str = ""
    why: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    shown: bool = True
    item_id: int | None = None

    @property
    def anchor_msg_id(self) -> int:
        """Куда ведёт ссылка: на ключевое сообщение, иначе на начало треда."""
        return self.key_msg_ids[0] if self.key_msg_ids else self.thread_id

    @property
    def is_offtopic(self) -> bool:
        """Личные новости и живой оффтоп — отдельным разделом дайджеста."""
        return self.kind in OFFTOPIC_KINDS

    @property
    def gist(self) -> str:
        """Суть одной строкой — для списка тем и карточек оценки."""
        return self.short or self.takeaway or self.decision

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Topic:
        known = {f for f in cls.__slots__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class DigestData:
    """Всё, что нужно для рендера. Собирается в pipeline."""

    chat_tg_id: int
    day: date_type
    chat_title: str = ""
    highlights: list[str] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    unanswered: list[tuple[str, int]] = field(default_factory=list)  # (вопрос, msg_id)
    links: list[str] = field(default_factory=list)
    msg_count: int = 0
    participants: int = 0
    noise_count: int = 0
    busiest_thread: Topic | None = None
    low_value_count: int = 0
    heroes: str = ""          # строка «🏅 Герои дня» (TZ §4.10)
    # выпуск-статья для сайта: {headline, lead, teaser: [...], stories: [...]}
    article: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_tg_id": self.chat_tg_id,
            "day": self.day.isoformat(),
            "chat_title": self.chat_title,
            "highlights": self.highlights,
            "topics": [t.to_dict() for t in self.topics],
            "unanswered": [[q, i] for q, i in self.unanswered],
            "links": self.links,
            "msg_count": self.msg_count,
            "participants": self.participants,
            "noise_count": self.noise_count,
            "low_value_count": self.low_value_count,
            "heroes": self.heroes,
            "article": self.article,
        }

    def titled(self, title: str | None) -> DigestData:
        """Тот же дайджест с нынешним названием группы.

        Дайджест, собранный до того, как стало известно название, хранит номер
        группы — показывать и публиковать его лучше с настоящим названием.
        """
        if title and title != self.chat_title:
            return replace(self, chat_title=title)
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DigestData:
        topics = [Topic.from_dict(t) for t in data.get("topics") or []]
        return cls(
            chat_tg_id=int(data.get("chat_tg_id", 0)),
            day=date_type.fromisoformat(str(data.get("day"))),
            chat_title=data.get("chat_title", ""),
            highlights=list(data.get("highlights") or []),
            topics=topics,
            unanswered=[(q, int(i)) for q, i in (data.get("unanswered") or [])],
            links=list(data.get("links") or []),
            msg_count=int(data.get("msg_count", 0)),
            participants=int(data.get("participants", 0)),
            noise_count=int(data.get("noise_count", 0)),
            busiest_thread=max(topics, key=lambda t: t.msg_count, default=None),
            low_value_count=int(data.get("low_value_count", 0)),
            heroes=str(data.get("heroes") or ""),
            article=dict(data.get("article") or {}),
        )


# ------------------------------------------------------------------ вид темы

OFFTOPIC_KINDS = frozenset({"life", "fun"})

KIND_MARKS = {
    "decision": "🟢",
    "insight": "💡",
    "resource": "🔗",
    "announcement": "📣",
    "question": "❓",
    "drama": "🔥",
    "life": "🎉",
    "fun": "😄",
    "other": "💬",
}


def _sides_line(topic: Topic, plain: Any) -> str:
    """Кто с кем спорил: «Вася — за Veo; Петя — за Kling»."""
    parts = []
    for side in topic.sides:
        who = str(side.get("who") or "").strip()
        stance = str(side.get("stance") or "").strip()
        if who and stance:
            parts.append(f"{plain(who)} — {plain(stance)}")
    if parts:
        return "; ".join(parts)
    return plain(topic.debate) if topic.debate else ""


def _meta_line(topic: Topic, chat_tg_id: int, link: Any) -> str:
    meta = []
    people = len(topic.participants) or len(topic.contributors)
    if people:
        meta.append(f"👥 {people}")
    meta.append(f"💬 {topic.msg_count}")
    if topic.reactions:
        meta.append(f"❤️ {topic.reactions}")
    anchor = link("к обсуждению", deeplink(chat_tg_id, topic.anchor_msg_id))
    return f"{' · '.join(meta)} · {anchor}"


def _topic_body(topic: Topic, chat_tg_id: int, *, link: Any, plain: Any) -> list[str]:
    """Содержимое темы: пересказ, кто спорил, итог, зачем знать, ссылка."""
    lines: list[str] = []
    summary = topic.summary or topic.takeaway or topic.decision
    if summary:
        lines.append(plain(summary))
    sides = _sides_line(topic, plain)
    if sides:
        lines.append(f"🗣 Спорили: {sides}")
    # итог отдельной строкой — только если пересказ сам его не содержит
    if topic.decision and topic.decision not in summary:
        lines.append(f"✅ Итог: {plain(topic.decision)}")
    if topic.why:
        lines.append(f"💡 Зачем знать: {plain(topic.why)}")
    if topic.mentions and not topic.is_offtopic:
        lines.append(f"🧩 Упоминали: {plain(', '.join(topic.mentions[:6]))}")
    lines.append(_meta_line(topic, chat_tg_id, link))
    return lines


def _stats_line(data: DigestData, plain: Any) -> str:
    parts = [f"📊 {data.msg_count} сообщений", f"{data.participants} участников"]
    if data.noise_count:
        parts.append(f"{data.noise_count} коротких реплик не в счёт")
    return ", ".join(parts)


def _sections(data: DigestData) -> tuple[list[Topic], list[Topic]]:
    work = [t for t in data.topics if not t.is_offtopic]
    offtopic = [t for t in data.topics if t.is_offtopic]
    return work, offtopic


# --------------------------------------------------------------------- Markdown

def render(data: DigestData) -> str:
    """Markdown для хранения и админки."""
    title = data.chat_title or str(data.chat_tg_id)
    bold = lambda t: f"*{esc_md(t)}*"  # noqa: E731
    lines: list[str] = [bold(f"Дайджест «{title}» за {data.day:%d.%m.%Y}"), ""]

    if not data.topics and not data.highlights:
        lines.append("За день ничего заметного не обсуждали.")
        lines.append(_stats_line(data, esc_md))
        return "\n".join(lines).strip()

    if data.highlights:
        lines.append("📌 " + bold("Главное за день"))
        lines += [f"• {esc_md(h)}" for h in data.highlights[:3]]
        lines.append("")

    work, offtopic = _sections(data)
    for heading, topics in (("", work), ("☕ Не по делу, но интересно", offtopic)):
        if not topics:
            continue
        if heading:
            lines += [bold(heading), ""]
        for topic in topics:
            lines.append(f"{KIND_MARKS.get(topic.kind, '💬')} {bold(topic.title)}")
            lines += _topic_body(topic, data.chat_tg_id, link=_md_link, plain=esc_md)
            lines.append("")

    if data.unanswered:
        lines.append("❓ " + bold("Без ответа"))
        for question, msg_id in data.unanswered[:5]:
            lines.append("• " + _md_link(question, deeplink(data.chat_tg_id, msg_id)))
        lines.append("")
    if data.links:
        lines.append("🔗 " + bold("Ссылки дня"))
        lines += [f"• {esc_md(url)}" for url in data.links[:10]]
        lines.append("")
    if data.heroes:
        lines += [esc_md(data.heroes), ""]
    lines.append(_stats_line(data, esc_md))
    return "\n".join(lines).strip()


def _md_link(text: str, url: str) -> str:
    return f"[{esc_md(text)}]({url})"


# ---------------------------------------------------------------- Telegram HTML

def _html_link(text: str, url: str) -> str:
    return f'<a href="{esc_html(url)}">{esc_html(text)}</a>'


def _expandable(lines: list[str]) -> str:
    """Свёрнутая цитата Telegram: видно начало, остальное — по нажатию."""
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def _topic_html(topic: Topic, chat_tg_id: int, limit: int = TELEGRAM_LIMIT) -> str:
    """Тема: заголовок и свёрнутые подробности. Слишком длинный пересказ
    укорачивается до рендера — так разметка остаётся целой."""
    head = f"{KIND_MARKS.get(topic.kind, '💬')} <b>{esc_html(topic.title)}</b>"
    while True:
        body = _topic_body(topic, chat_tg_id, link=_html_link, plain=esc_html)
        block = f"{head}\n{_expandable(body)}"
        text = topic.summary
        if len(block) <= limit or len(text) < 50:
            return block
        excess = len(block) - limit
        cut = text[: max(0, len(text) - excess - 10)].rsplit(" ", 1)[0]
        topic = replace(topic, summary=cut.rstrip(" ,.;:") + "…")


def _fit(block: str, limit: int) -> str:
    """Блок длиннее лимита (очень длинная тема) — обрезаем текст, а не разметку."""
    if len(block) <= limit:
        return block
    closing = "</blockquote>"
    if block.endswith(closing):
        room = limit - len(closing) - 1
        opening = block.find("<blockquote expandable>") + len("<blockquote expandable>")
        cut = block[:room].rfind("\n")
        return block[: max(cut, opening)] + "…" + closing
    return block[:limit - 1].rsplit("\n", 1)[0] + "…"


def pack_blocks(blocks: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Сложить блоки в сообщения не длиннее лимита, не разрывая ни один блок."""
    messages: list[str] = []
    current = ""
    for block in (_fit(b, limit) for b in blocks if b):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            messages.append(current)
        current = block
    if current:
        messages.append(current)
    return messages


def digest_blocks(data: DigestData, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Дайджест по блокам: шапка, темы, оффтоп, вопросы, ссылки, итоги дня."""
    title = data.chat_title or str(data.chat_tg_id)
    head = [f"<b>📰 Дайджест «{esc_html(title)}» за {data.day:%d.%m.%Y}</b>"]

    if not data.topics and not data.highlights:
        head += ["", "За день ничего заметного не обсуждали."]
        tail = [esc_html(data.heroes)] if data.heroes else []
        return ["\n".join(head), *tail, _stats_line(data, esc_html)]

    if data.highlights:
        head += ["", "📌 <b>Главное за день</b>"]
        head += [f"• {esc_html(h)}" for h in data.highlights[:3]]

    blocks = ["\n".join(head)]
    work, offtopic = _sections(data)
    blocks += [_topic_html(t, data.chat_tg_id, limit) for t in work]
    if offtopic:
        blocks.append("☕ <b>Не по делу, но интересно</b>")
        blocks += [_topic_html(t, data.chat_tg_id, limit) for t in offtopic]

    if data.unanswered:
        items = [
            "• " + _html_link(q, deeplink(data.chat_tg_id, msg_id))
            for q, msg_id in data.unanswered[:5]
        ]
        blocks.append("❓ <b>Остались без ответа</b>\n" + "\n".join(items))
    if data.links:
        items = [f"• {esc_html(url)}" for url in data.links[:10]]
        blocks.append("🔗 <b>Ссылки дня</b>\n" + _expandable(items))

    tail = [esc_html(data.heroes)] if data.heroes else []
    tail.append(_stats_line(data, esc_html))
    blocks.append("\n\n".join(tail))
    return blocks


def digest_messages(data: DigestData, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Дайджест для Telegram: одно сообщение, а если не влезает — несколько."""
    return pack_blocks(digest_blocks(data, limit), limit)


def render_html(data: DigestData) -> str:
    """Весь дайджест одним HTML-текстом — для предпросмотра и проверок."""
    return "\n\n".join(digest_blocks(data))


def topic_card(topic: Topic, chat_tg_id: int) -> str:
    """Короткая карточка темы для оценки владельцем: заголовок, суть, ссылка."""
    lines = [f"{KIND_MARKS.get(topic.kind, '💬')} <b>{esc_html(topic.title)}</b>"]
    if topic.gist:
        lines.append(esc_html(topic.gist))
    lines.append(_html_link("к обсуждению", deeplink(chat_tg_id, topic.anchor_msg_id)))
    return "\n".join(lines)


# ----------------------------------------------------------- короткий пост в чат

HASHTAG = "#дайджест"


TEASER_STORIES = 6


def story_anchor(thread_id: int) -> str:
    """Якорь темы на странице выпуска: по нему страница раскроет именно её."""
    return f"t{thread_id}"


def teaser_html(data: DigestData, url: str) -> str:
    """Короткий пост в группу: заголовок, лид, заголовки новостей ссылками, #дайджест.

    Каждая новость — ссылка на свою тему на странице выпуска: там она сразу
    раскрыта, а остальные свёрнуты. Подробностей в самом посте нет — чат не
    засыпается простынёй.
    """
    article = data.article or {}
    headline = str(article.get("headline") or "") or f"Дайджест за {data.day:%d.%m}"
    lines = [f"📰 <b>{esc_html(headline)}</b>"]
    lead = str(article.get("lead") or "")
    if lead:
        lines += ["", esc_html(lead)]

    by_thread = {t.thread_id: t for t in data.topics}
    items: list[tuple[int, str]] = []
    for story in article.get("stories") or []:
        thread_id = story.get("thread_id")
        if thread_id in by_thread and str(story.get("headline") or "").strip():
            items.append((int(thread_id), str(story["headline"])))
    if not items:   # статьи нет — заголовки тем дня, рабочие вперёд
        ordered = sorted(data.topics, key=lambda t: t.is_offtopic)
        items = [(t.thread_id, t.title) for t in ordered if t.title]

    if items:
        lines.append("")
        lines += [
            f"▸ {_html_link(title, f'{url}#{story_anchor(thread_id)}')}"
            for thread_id, title in items[:TEASER_STORIES]
        ]
        if len(items) > TEASER_STORIES:
            lines.append(f"…и ещё {len(items) - TEASER_STORIES}")
    lines += ["", _html_link("Весь выпуск на сайте →", url), "", HASHTAG]
    return "\n".join(lines)
