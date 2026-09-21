"""Рендер дайджеста (TZ §4.3, пункты 5-6).

Два формата из одних данных:
  * Markdown — для хранения и веб-админки;
  * Telegram HTML — для отправки в личку. HTML выбран потому, что в нём
    экранируется ровно три символа, а Markdown Telegram ломается на любой
    звёздочке или подчёркивании внутри текста, пришедшего от модели.

Структура дайджеста целиком складывается в `digests.topics`, чтобы команда
`/digest <дата>` за прошедший день собирала ровно ту же картинку, а не
пыталась превращать хранимый текст обратно в разметку.
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import asdict, dataclass, field
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
    decision: str = ""
    debate: str = ""
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
        }

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
        )


# --------------------------------------------------------------------- Markdown

def render(data: DigestData) -> str:
    """Markdown для хранения и админки."""
    return _render(data, bold=lambda t: f"*{esc_md(t)}*", link=_md_link, plain=esc_md)


def render_html(data: DigestData) -> str:
    """Telegram HTML для отправки в личку."""
    return _render(
        data, bold=lambda t: f"<b>{esc_html(t)}</b>", link=_html_link, plain=esc_html
    )


def _md_link(text: str, url: str) -> str:
    return f"[{esc_md(text)}]({url})"


def _html_link(text: str, url: str) -> str:
    return f'<a href="{esc_html(url)}">{esc_html(text)}</a>'


def _render(data: DigestData, *, bold: Any, link: Any, plain: Any) -> str:
    title = data.chat_title or str(data.chat_tg_id)
    lines: list[str] = [bold(f"Дайджест «{title}» за {data.day:%d.%m.%Y}"), ""]

    if not data.topics and not data.highlights:
        lines.append("За день ничего заметного не обсуждали.")
        lines += _stats_block(data, plain)
        return "\n".join(lines).strip()

    if data.highlights:
        lines.append("📌 " + bold("Главное за день"))
        lines += [f"• {plain(h)}" for h in data.highlights[:3]]
        lines.append("")

    for topic in data.topics:
        lines += _topic_block(topic, data.chat_tg_id, bold=bold, link=link, plain=plain)

    if data.unanswered:
        lines.append("❓ " + bold("Без ответа"))
        for question, msg_id in data.unanswered[:5]:
            lines.append("• " + link(question, deeplink(data.chat_tg_id, msg_id)))
        lines.append("")

    if data.links:
        lines.append("🔗 " + bold("Ссылки дня"))
        lines += [f"• {plain(url)}" for url in data.links[:10]]
        lines.append("")

    lines += _stats_block(data, plain)
    return "\n".join(lines).strip()


KIND_MARKS = {
    "decision": "🟢 [Решение]",
    "insight": "💡 [Вывод]",
    "resource": "🔗 [Ресурс]",
    "announcement": "📣 [Анонс]",
    "question": "❓ [Вопрос]",
    "drama": "🔥 [Спор]",
    "other": "•",
}


def digest_parts(data: DigestData) -> list[tuple[str, Topic | None]]:
    """Разбить дайджест на сообщения: шапка, каждая тема отдельно, хвост.

    Тема уходит своим сообщением, потому что кнопки 👍 👎 🔕 привязываются к
    конкретной теме (TZ §4.7), а в Telegram клавиатура принадлежит сообщению.
    """
    title = data.chat_title or str(data.chat_tg_id)
    head = [f"<b>Дайджест «{esc_html(title)}» за {data.day:%d.%m.%Y}</b>"]

    if not data.topics and not data.highlights:
        head += ["", "За день ничего заметного не обсуждали.", ""]
        head += _stats_block(data, esc_html)
        return [("\n".join(head), None)]

    if data.highlights:
        head += ["", "📌 <b>Главное за день</b>"]
        head += [f"• {esc_html(h)}" for h in data.highlights[:3]]

    parts: list[tuple[str, Topic | None]] = [("\n".join(head), None)]

    for topic in data.topics:
        block = _topic_block(
            topic, data.chat_tg_id,
            bold=lambda t: f"<b>{esc_html(t)}</b>", link=_html_link, plain=esc_html,
        )
        parts.append(("\n".join(block).strip(), topic))

    tail: list[str] = []
    if data.unanswered:
        tail += ["❓ <b>Без ответа</b>"]
        for question, msg_id in data.unanswered[:5]:
            tail.append("• " + _html_link(question, deeplink(data.chat_tg_id, msg_id)))
        tail.append("")
    if data.links:
        tail += ["🔗 <b>Ссылки дня</b>"]
        tail += [f"• {esc_html(url)}" for url in data.links[:10]]
        tail.append("")
    tail += _stats_block(data, esc_html)
    parts.append(("\n".join(tail).strip(), None))
    return parts


def _topic_block(topic: Topic, chat_tg_id: int, *, bold: Any, link: Any, plain: Any) -> list[str]:
    """Формат темы по §4.7: вывод и «почему важно», а не пересказ."""
    mark = KIND_MARKS.get(topic.kind, "•")
    lines = [f"{mark} {bold(topic.title)}"]

    # takeaway из рубрики точнее «решения» из map-стадии: он написан как вывод
    verdict = topic.takeaway or topic.decision
    if verdict:
        lines.append(f"Вывод: {plain(verdict)}")
    if topic.why:
        lines.append(f"Почему важно: {plain(topic.why)}")
    if topic.debate:
        lines.append(f"Спорили: {plain(topic.debate)}")
    if topic.mentions:
        lines.append(f"Упоминали: {plain(', '.join(topic.mentions[:6]))}")

    meta = [
        f"👥 {len(topic.participants) or len(topic.contributors)}",
        f"💬 {topic.msg_count}",
    ]
    if topic.reactions:
        meta.append(f"❤️ {topic.reactions}")
    anchor = link("↗️ к обсуждению", deeplink(chat_tg_id, topic.anchor_msg_id))
    lines.append(f"{' · '.join(meta)} · {anchor}")
    lines.append("")
    return lines


def _stats_block(data: DigestData, plain: Any) -> list[str]:
    parts = [f"📊 {data.msg_count} сообщений", f"{data.participants} участников"]
    if data.noise_count:
        parts.append(f"{data.noise_count} коротких реплик не в счёт")
    if data.busiest_thread:
        parts.append(f"самый активный тред — «{plain(data.busiest_thread.title)}»")
    return [", ".join(parts)]
