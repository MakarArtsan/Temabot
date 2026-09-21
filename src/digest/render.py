"""Рендер дайджеста в Markdown с дип-линками (TZ §4.3, пункты 5-6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type

SUPERGROUP_PREFIX = 1_000_000_000_000


def deeplink(chat_tg_id: int, tg_msg_id: int) -> str:
    """Ссылка на сообщение супергруппы (TZ §4.3 п.6).

    У супергрупп id вида -100XXXXXXXXXX, а в ссылке нужен внутренний номер
    без этой приставки.
    """
    internal = abs(chat_tg_id) - SUPERGROUP_PREFIX
    return f"https://t.me/c/{internal}/{tg_msg_id}"


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

    @property
    def anchor_msg_id(self) -> int:
        """Куда ведёт ссылка: на ключевое сообщение, иначе на начало треда."""
        return self.key_msg_ids[0] if self.key_msg_ids else self.thread_id


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


def render(data: DigestData) -> str:
    """Собрать Markdown дайджеста."""
    title = data.chat_title or str(data.chat_tg_id)
    lines: list[str] = [f"*Дайджест «{title}» за {data.day:%d.%m.%Y}*", ""]

    if not data.topics and not data.highlights:
        lines.append("_За день ничего заметного не обсуждали._")
        lines += _stats_block(data)
        return "\n".join(lines).strip()

    if data.highlights:
        lines.append("📌 *Главное за день*")
        lines += [f"• {h}" for h in data.highlights[:3]]
        lines.append("")

    for topic in data.topics:
        lines += _topic_block(topic, data.chat_tg_id)

    if data.unanswered:
        lines.append("❓ *Без ответа*")
        for question, msg_id in data.unanswered[:5]:
            lines.append(f"• [{question}]({deeplink(data.chat_tg_id, msg_id)})")
        lines.append("")

    if data.links:
        lines.append("🔗 *Ссылки дня*")
        lines += [f"• {link}" for link in data.links[:10]]
        lines.append("")

    lines += _stats_block(data)
    return "\n".join(lines).strip()


def _topic_block(topic: Topic, chat_tg_id: int) -> list[str]:
    link = deeplink(chat_tg_id, topic.anchor_msg_id)
    lines = [f"*{topic.title}*"]

    if topic.decision:
        lines.append(f"Вывод: {topic.decision}")
    if topic.debate:
        lines.append(f"Спорили: {topic.debate}")
    if topic.mentions:
        lines.append(f"Упоминали: {', '.join(topic.mentions[:6])}")

    meta = [f"👥 {len(topic.participants) or len(topic.contributors)}", f"💬 {topic.msg_count}"]
    if topic.reactions:
        meta.append(f"❤️ {topic.reactions}")
    lines.append(f"{' · '.join(meta)} · [↗️ к обсуждению]({link})")
    lines.append("")
    return lines


def _stats_block(data: DigestData) -> list[str]:
    parts = [f"📊 {data.msg_count} сообщений", f"{data.participants} участников"]
    if data.noise_count:
        parts.append(f"{data.noise_count} коротких реплик не в счёт")
    if data.busiest_thread:
        parts.append(f"самый активный тред — «{data.busiest_thread.title}»")
    return ["_" + ", ".join(parts) + "_"]
