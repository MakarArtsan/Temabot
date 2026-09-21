"""Сегментация сообщений на треды (TZ §4.2).

Правила по порядку:
1. topic_id форумной группы — жёсткая граница, тред не может пересечь тему;
2. reply-цепочки склеиваются транзитивно;
3. сообщение без ответа приклеивается к последнему активному треду, если прошло
   меньше 25 минут и его автор уже участвует в этом треде;
4. иначе — новый тред;
5. тред короче 3 сообщений и без вопросительных знаков помечается low_value.

Функция чистая: ни сети, ни БД, поэтому проверяется тестами целиком.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.db.models import Message

GAP_MINUTES = 25
MIN_THREAD_SIZE = 3


@dataclass(slots=True)
class Thread:
    """Обсуждение: несколько сообщений, связанных по смыслу."""

    chat_id: int
    root_msg_id: int              # tg_msg_id первого сообщения, он же thread_id
    messages: list[Message] = field(default_factory=list)
    topic_id: int | None = None

    @property
    def msg_ids(self) -> list[int]:
        return [m.tg_msg_id for m in self.messages]

    @property
    def participants(self) -> set[int]:
        return {m.tg_user_id for m in self.messages if m.tg_user_id is not None}

    @property
    def started_at(self) -> datetime:
        return self.messages[0].date

    @property
    def last_at(self) -> datetime:
        return self.messages[-1].date

    @property
    def duration_min(self) -> float:
        return (self.last_at - self.started_at).total_seconds() / 60

    @property
    def has_question(self) -> bool:
        return any("?" in m.content for m in self.messages)

    @property
    def low_value(self) -> bool:
        """Короткая перекличка без вопросов — в дайджест только строкой «прочее»."""
        return len(self.messages) < MIN_THREAD_SIZE and not self.has_question

    @property
    def text(self) -> str:
        lines = []
        for m in self.messages:
            author = m.author_name or (str(m.tg_user_id) if m.tg_user_id else "аноним")
            body = m.content or (f"[{m.media_type}]" if m.media_type else "")
            lines.append(f"[{m.tg_msg_id}] {author}: {body}")
        return "\n".join(lines)


class _Groups:
    """Система непересекающихся множеств по tg_msg_id.

    Родитель может отсутствовать в выборке (ответ на вчерашнее сообщение) — тогда
    его id всё равно участвует как узел, и все ответы на него оказываются вместе.
    """

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # сжатие пути
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            # меньший id — корень: тред называется по своему первому сообщению
            if right_root < left_root:
                left_root, right_root = right_root, left_root
            self._parent[right_root] = left_root


def segment(
    messages: list[Message], *, gap_minutes: int = GAP_MINUTES
) -> list[Thread]:
    """Разложить сообщения по тредам. Порядок результата — по времени начала."""
    if not messages:
        return []

    ordered = sorted(messages, key=lambda m: (m.date, m.tg_msg_id))
    groups = _Groups()
    known_ids = {m.tg_msg_id for m in ordered}

    # правило 2: reply-цепочки
    for msg in ordered:
        groups.find(msg.tg_msg_id)
        if msg.reply_to:
            groups.union(msg.tg_msg_id, msg.reply_to)

    # правила 3-4: приклеивание по времени и участникам, внутри своей темы
    last_seen: dict[int | None, tuple[int, datetime, set[int]]] = {}
    gap = timedelta(minutes=gap_minutes)

    for msg in ordered:
        topic = msg.topic_id
        previous = last_seen.get(topic)
        root = groups.find(msg.tg_msg_id)

        replied_inside = bool(msg.reply_to) and msg.reply_to in known_ids
        if previous and not replied_inside:
            prev_root, prev_date, prev_authors = previous
            close_enough = msg.date - prev_date < gap
            same_people = msg.tg_user_id is not None and msg.tg_user_id in prev_authors
            if close_enough and same_people:
                groups.union(msg.tg_msg_id, prev_root)
                root = groups.find(msg.tg_msg_id)

        authors = previous[2] if previous and groups.find(previous[0]) == root else set()
        if msg.tg_user_id is not None:
            authors = authors | {msg.tg_user_id}
        last_seen[topic] = (root, msg.date, authors)

    # собираем треды
    buckets: dict[int, Thread] = {}
    for msg in ordered:
        root = groups.find(msg.tg_msg_id)
        thread = buckets.get(root)
        if thread is None:
            thread = Thread(chat_id=msg.chat_id, root_msg_id=root, topic_id=msg.topic_id)
            buckets[root] = thread
        thread.messages.append(msg)

    threads = list(buckets.values())
    for thread in threads:
        # корнем мог оказаться id сообщения не из выборки (ответ на вчерашнее)
        if thread.root_msg_id not in known_ids:
            thread.root_msg_id = thread.messages[0].tg_msg_id
    threads.sort(key=lambda t: (t.started_at, t.root_msg_id))
    return threads
