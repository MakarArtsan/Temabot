"""Датаклассы строк БД (TZ §3). SQL живёт только в src/db/repo.py."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

CopierMode = str  # allow | deny | ask
ContribRole = str  # initiator | key | answerer


def _as_dict(value: Any) -> dict[str, Any]:
    """jsonb приходит dict'ом (см. pool._init_connection), но бывает и строкой."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    import json

    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


@dataclass(slots=True)
class Chat:
    id: int
    tg_id: int
    title: str | None = None
    is_protected: bool = False
    collect: bool = False
    digest: bool = False
    copier: CopierMode = "ask"
    digest_time: time | None = None
    retention_days: int = 365
    settings: dict[str, Any] = field(default_factory=dict)
    added_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Chat:
        return cls(
            id=row["id"],
            tg_id=row["tg_id"],
            title=row["title"],
            is_protected=row["is_protected"],
            collect=row["collect"],
            digest=row["digest"],
            copier=row["copier"],
            digest_time=row["digest_time"],
            retention_days=row["retention_days"],
            settings=_as_dict(row["settings"]),
            added_at=row["added_at"],
        )


@dataclass(slots=True)
class Author:
    tg_user_id: int
    name: str | None = None
    weight: float = 1.0
    muted: bool = False
    hide_from_ratings: bool = False

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Author:
        return cls(
            tg_user_id=row["tg_user_id"],
            name=row["name"],
            weight=row["weight"],
            muted=row["muted"],
            hide_from_ratings=row["hide_from_ratings"],
        )


@dataclass(slots=True)
class Message:
    """Строка messages. `chat_id` — внутренний id чата, не tg_id."""

    chat_id: int
    tg_msg_id: int
    date: datetime
    id: int | None = None
    tg_user_id: int | None = None
    author_name: str | None = None
    text: str | None = None
    transcript: str | None = None
    media_type: str | None = None
    media_path: str | None = None
    reply_to: int | None = None
    topic_id: int | None = None
    thread_id: int | None = None
    source: str = "collector"
    is_pinned_by_me: bool = False
    copy_count: int = 0
    reactions: int = 0
    reply_count: int = 0
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def content(self) -> str:
        """Текст для анализа: расшифровка голосового равноправна тексту."""
        parts = [p for p in (self.text, self.transcript) if p]
        return "\n".join(parts)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Message:
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            tg_msg_id=row["tg_msg_id"],
            tg_user_id=row["tg_user_id"],
            author_name=row["author_name"],
            text=row["text"],
            transcript=row["transcript"],
            media_type=row["media_type"],
            media_path=row["media_path"],
            reply_to=row["reply_to"],
            topic_id=row["topic_id"],
            thread_id=row["thread_id"],
            source=row["source"],
            is_pinned_by_me=row["is_pinned_by_me"],
            copy_count=row["copy_count"],
            reactions=row["reactions"],
            reply_count=row["reply_count"],
            date=row["date"],
            edited_at=row["edited_at"],
            deleted_at=row["deleted_at"],
            raw=_as_dict(row["raw"]),
        )


@dataclass(slots=True)
class Digest:
    chat_id: int
    day: date
    summary_md: str
    id: int | None = None
    topics: list[dict[str, Any]] = field(default_factory=list)
    msg_count: int = 0
    tokens_used: int = 0
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Digest:
        topics = row["topics"]
        if isinstance(topics, str):
            import json

            topics = json.loads(topics)
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            day=row["day"],
            summary_md=row["summary_md"],
            topics=list(topics or []),
            msg_count=row["msg_count"],
            tokens_used=row["tokens_used"],
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class DigestItem:
    chat_id: int
    thread_id: int | None
    title: str
    kind: str
    score: float
    shown: bool
    id: int | None = None
    digest_id: int | None = None
    features: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> DigestItem:
        return cls(
            id=row["id"],
            digest_id=row["digest_id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            title=row["title"],
            kind=row["kind"],
            features=_as_dict(row["features"]),
            score=row["score"],
            shown=row["shown"],
        )
