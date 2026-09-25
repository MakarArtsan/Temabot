"""Расшифровка голосовых силами Telegram (для аккаунтов с Premium).

У Premium есть встроенная расшифровка голосовых и кружков. Через MTProto её
можно попросить самому: метод `messages.transcribeAudio`. Это заметно выгоднее
локального whisper — не нужно ни скачивать файл, ни тратить процессор и память.

Ответ приходит не сразу: сначала Telegram отвечает `pending = True`, а текст
досылает позже. Поэтому запрос повторяется с растущей паузой, пока не появится
текст или не кончится терпение.

Если Premium нет, лимит исчерпан или запись слишком длинная — возвращаем None,
и вызывающий откатывается на локальный whisper.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import (
    FloodPremiumWaitError,
    PremiumAccountRequiredError,
)
from telethon.tl.functions.messages import TranscribeAudioRequest

from src.config import cfg

log = logging.getLogger(__name__)

POLL_DELAYS = (2, 3, 5, 8, 13, 21, 34)   # растущие паузы, суммарно около полутора минут

# Один раз выясняем, что Premium нет, и больше не тратим запросы
_premium_missing = False


def reset() -> None:
    """Для тестов и смены аккаунта."""
    global _premium_missing
    _premium_missing = False


def voice_duration(message: Any) -> int:
    """Длительность записи в секундах, 0 — если неизвестна."""
    document = getattr(message, "document", None)
    for attribute in getattr(document, "attributes", None) or []:
        duration = getattr(attribute, "duration", None)
        if duration:
            return int(duration)
    return 0


def is_too_long(message: Any, limit_sec: int | None = None) -> bool:
    """Telegram расшифровывает только короткие записи."""
    limit = limit_sec if limit_sec is not None else cfg.TELEGRAM_ASR_MAX_SEC
    duration = voice_duration(message)
    return bool(duration and duration > limit)


async def transcribe_message(
    message: Any, *, max_wait_sec: int | None = None, sleep: Any = asyncio.sleep
) -> str | None:
    """Попросить Telegram расшифровать голосовое.

    Возвращает текст, либо None — если расшифровать не удалось и нужен whisper.
    """
    global _premium_missing

    if _premium_missing:
        return None
    if is_too_long(message):
        log.info(
            "Запись длиннее %s сек — Telegram её не расшифрует", cfg.TELEGRAM_ASR_MAX_SEC
        )
        return None

    client = getattr(message, "client", None)
    if client is None:
        log.warning("У сообщения нет клиента — расшифровка силами Telegram недоступна")
        return None

    budget = max_wait_sec if max_wait_sec is not None else cfg.TELEGRAM_ASR_WAIT_SEC
    spent = 0

    for delay in (0, *POLL_DELAYS):
        if delay:
            if spent + delay > budget:
                break
            await sleep(delay)
            spent += delay

        try:
            result = await client(
                TranscribeAudioRequest(peer=message.peer_id, msg_id=message.id)
            )
        except PremiumAccountRequiredError:
            # запоминаем: у аккаунта нет Premium, дальше даже не пробуем
            _premium_missing = True
            log.info("У аккаунта нет Premium — расшифровываем локально")
            return None
        except FloodPremiumWaitError as exc:
            log.warning("Лимит расшифровок Telegram исчерпан: %s", exc)
            return None
        except FloodWaitError as exc:
            log.warning("FloodWait %s сек на расшифровке — отдаём whisper", exc.seconds)
            return None
        except Exception:
            log.warning("Telegram не смог расшифровать сообщение %s", message.id,
                        exc_info=True)
            return None

        text = (getattr(result, "text", "") or "").strip()
        if not getattr(result, "pending", False):
            # пустой текст без pending означает «речи не нашлось», а не ошибку
            return text
        if text:
            # промежуточный результат уже есть, но расшифровка ещё идёт
            log.debug("Расшифровка ещё не закончена, ждём")

    log.info("Telegram не успел расшифровать сообщение %s за %s сек", message.id, budget)
    return None
