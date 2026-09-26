"""Публикация дайджеста в саму группу (решение владельца, см. TZ §9).

У каждой группы режим `chats.publish`:
  * off    — дайджест видит только владелец: в личке и в админке (по умолчанию);
  * manual — владелец сначала читает сам и публикует кнопкой в личке или в админке;
  * auto   — сразу после вечерней сборки, раз в сутки.

Дайджест группы уходит только в эту же группу: её участники и так видели
исходные сообщения. Кнопок оценки в группе нет — они про вкус владельца, а
строка «Герои дня» с именами попадает туда, только если в группе включена
публикация рейтингов: на неё нужно отдельное согласие админов (TZ §4.10).
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Any

from src.db import repo
from src.db.models import Chat
from src.digest.render import DigestData, deeplink, render_html, split_message

log = logging.getLogger(__name__)

PUBLISH_MODES = ("off", "manual", "auto")
PUBLISH_LABELS = {
    "off": "только мне",
    "manual": "по кнопке",
    "auto": "автоматически",
}


@dataclass(slots=True)
class PublishResult:
    ok: bool
    text: str                       # что показать владельцу
    skipped: bool = False           # публиковать было нечего или незачем — не ошибка
    message_ids: list[int] = field(default_factory=list)
    link: str = ""                  # ссылка на пост в группе


def is_empty(data: DigestData) -> bool:
    """«За день ничего заметного» в группу не шлём: это шум, а не новость."""
    return not data.topics and not data.highlights


def ratings_are_public(chat: Chat) -> bool:
    from src.bot.handlers_ratings import ratings_are_public as check

    return check(chat)


def group_parts(data: DigestData, *, ratings_public: bool) -> list[str]:
    """Текст для группы: тот же дайджест сплошным текстом, без кнопок оценки."""
    if data.heroes and not ratings_public:
        data = dataclasses.replace(data, heroes="")
    return split_message(render_html(data))


def group_preview(digest_payload: dict[str, Any], chat: Chat) -> list[str]:
    """Как дайджест будет выглядеть в группе — для предпросмотра в админке."""
    if not digest_payload:
        return []
    data = DigestData.from_dict(digest_payload).titled(chat.title)
    return group_parts(data, ratings_public=ratings_are_public(chat))


def can_offer(chat: Chat | None, digest: Any) -> bool:
    """Показывать ли владельцу кнопку «Опубликовать в группе»."""
    return bool(
        chat is not None
        and chat.publish in ("manual", "auto")
        and digest is not None
        and getattr(digest, "published_at", None) is None
        and getattr(digest, "payload", None)
        and not is_empty(DigestData.from_dict(digest.payload))
    )


async def publish_digest(bot: Any, digest_id: int, *, auto: bool = False) -> PublishResult:
    """Опубликовать дайджест в его группе. Повторный вызов ничего не задублирует."""
    digest = await repo.get_digest_by_id(digest_id)
    if digest is None:
        return PublishResult(False, "Дайджест не найден")
    chat = await repo.get_chat_by_id(digest.chat_id)
    if chat is None:
        return PublishResult(False, "Группа не найдена")
    title = chat.title or str(chat.tg_id)

    if chat.publish == "off":
        return PublishResult(
            False, f"Публикация в «{title}» выключена — включи её в админке, «Группы»"
        )
    if auto and chat.publish != "auto":
        return PublishResult(False, "Автопубликация выключена", skipped=True)
    if not digest.payload:
        return PublishResult(False, "У дайджеста нет сохранённой структуры — пересобери его")

    data = DigestData.from_dict(digest.payload).titled(chat.title)
    if is_empty(data):
        return PublishResult(False, "За день ничего заметного — публиковать нечего", skipped=True)
    if not await repo.claim_digest_publication(digest_id):
        return PublishResult(False, "Этот дайджест уже опубликован", skipped=True)

    sent: list[int] = []
    try:
        for part in group_parts(data, ratings_public=ratings_are_public(chat)):
            message = await bot.send_message(
                chat.tg_id,
                part,
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                # дайджест приходит поздно вечером — без звука у всех участников
                disable_notification=True,
            )
            sent.append(int(message.message_id))
    except Exception as exc:
        log.exception("Не удалось опубликовать дайджест %s в группу %s", digest_id, chat.tg_id)
        if sent:
            # начало уже в группе: отметку оставляем, иначе повтор задублирует его
            await repo.finish_digest_publication(digest_id, sent)
            return PublishResult(
                False, f"Опубликован не полностью: {exc}", message_ids=sent,
                link=deeplink(chat.tg_id, sent[0]),
            )
        await repo.release_digest_publication(digest_id)
        return PublishResult(False, f"Telegram не принял сообщение: {exc}")

    await repo.finish_digest_publication(digest_id, sent)
    log.info("Дайджест %s опубликован в группе %s", digest_id, chat.tg_id)
    return PublishResult(
        True, f"Опубликовал в «{title}»", message_ids=sent, link=deeplink(chat.tg_id, sent[0])
    )


async def unpublish_digest(bot: Any, digest_id: int) -> PublishResult:
    """Убрать опубликованный дайджест из группы.

    Telegram разрешает боту удалять свои сообщения только первые 48 часов.
    """
    digest = await repo.get_digest_by_id(digest_id)
    if digest is None or digest.published_at is None:
        return PublishResult(False, "Этот дайджест в группе не публиковался")
    chat = await repo.get_chat_by_id(digest.chat_id)
    if chat is None:
        return PublishResult(False, "Группа не найдена")

    if digest.published_msg_ids:
        try:
            await bot.delete_messages(chat.tg_id, digest.published_msg_ids)
        except Exception as exc:
            log.exception("Не удалось убрать дайджест %s из группы", digest_id)
            return PublishResult(
                False,
                f"Не получилось удалить: {exc}. Бот может удалять свои сообщения "
                "только первые 48 часов — дальше удали пост в группе вручную",
            )
    await repo.release_digest_publication(digest_id)
    return PublishResult(True, f"Убрал из «{chat.title or chat.tg_id}»")
