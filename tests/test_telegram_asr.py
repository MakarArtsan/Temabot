"""Тесты расшифровки силами Telegram Premium."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from src.config import cfg
from src.media import telegram_asr as tg_asr
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import (
    FloodPremiumWaitError,
    PremiumAccountRequiredError,
)


@pytest.fixture(autouse=True)
def fresh() -> None:
    """Модуль запоминает отсутствие Premium — между тестами это надо сбрасывать."""
    tg_asr.reset()


class FakeClient:
    """Клиент, отдающий заранее заданную цепочку ответов."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, request: Any) -> Any:
        self.calls += 1
        answer = self.responses.pop(0) if self.responses else self.responses
        if isinstance(answer, Exception):
            raise answer
        return answer


def answer(text: str = "", *, pending: bool = False) -> SimpleNamespace:
    return SimpleNamespace(transcription_id=1, text=text, pending=pending)


def voice(duration: int = 30, *, responses: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        peer_id=SimpleNamespace(channel_id=123),
        client=FakeClient(responses or [answer("расшифровка")]),
        document=SimpleNamespace(
            attributes=[SimpleNamespace(duration=duration, voice=True)]
        ),
    )


async def no_sleep(seconds: float) -> None:
    return None


# ------------------------------------------------------------- удачный путь

async def test_transcription_comes_back():
    message = voice(responses=[answer("привет из голосового")])

    text = await tg_asr.transcribe_message(message, sleep=no_sleep)

    assert text == "привет из голосового"
    assert message.client.calls == 1, "одного запроса хватило"


async def test_pending_result_is_awaited():
    """Сначала Telegram отвечает «ещё считаю», текст присылает позже."""
    message = voice(responses=[
        answer(pending=True),
        answer(pending=True),
        answer("готовая расшифровка"),
    ])

    text = await tg_asr.transcribe_message(message, sleep=no_sleep)

    assert text == "готовая расшифровка"
    assert message.client.calls == 3


async def test_silence_is_not_an_error():
    """Пустой текст без pending означает «речи не нашлось»."""
    message = voice(responses=[answer("")])
    assert await tg_asr.transcribe_message(message, sleep=no_sleep) == ""


# ------------------------------------------------------- когда нужен whisper

async def test_no_premium_falls_back():
    message = voice(responses=[PremiumAccountRequiredError(request=None)])

    assert await tg_asr.transcribe_message(message, sleep=no_sleep) is None


async def test_missing_premium_is_remembered():
    """Один отказ — и больше не тратим запросы на каждое голосовое."""
    first = voice(responses=[PremiumAccountRequiredError(request=None)])
    await tg_asr.transcribe_message(first, sleep=no_sleep)

    second = voice(responses=[answer("не должно запрашиваться")])
    assert await tg_asr.transcribe_message(second, sleep=no_sleep) is None
    assert second.client.calls == 0

    tg_asr.reset()
    third = voice(responses=[answer("снова работает")])
    assert await tg_asr.transcribe_message(third, sleep=no_sleep) == "снова работает"


async def test_premium_limit_falls_back():
    message = voice(responses=[FloodPremiumWaitError(request=None)])
    assert await tg_asr.transcribe_message(message, sleep=no_sleep) is None


async def test_flood_wait_falls_back():
    error = FloodWaitError(request=None)
    error.seconds = 300
    message = voice(responses=[error])

    assert await tg_asr.transcribe_message(message, sleep=no_sleep) is None


async def test_unexpected_error_falls_back():
    message = voice(responses=[RuntimeError("что-то пошло не так")])
    assert await tg_asr.transcribe_message(message, sleep=no_sleep) is None


async def test_long_recording_is_not_sent(monkeypatch: pytest.MonkeyPatch):
    """Telegram расшифровывает только короткие записи — длинные не тревожим."""
    monkeypatch.setattr(cfg, "TELEGRAM_ASR_MAX_SEC", 180)
    message = voice(duration=600, responses=[answer("не должно запрашиваться")])

    assert await tg_asr.transcribe_message(message, sleep=no_sleep) is None
    assert message.client.calls == 0


async def test_recording_at_the_limit_is_sent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "TELEGRAM_ASR_MAX_SEC", 180)
    message = voice(duration=180, responses=[answer("ровно три минуты")])

    assert await tg_asr.transcribe_message(message, sleep=no_sleep) == "ровно три минуты"


async def test_unknown_duration_is_still_tried():
    """Длительности может не быть — пробуем, Telegram сам откажет если что."""
    message = voice(responses=[answer("текст")])
    message.document = None

    assert await tg_asr.transcribe_message(message, sleep=no_sleep) == "текст"


async def test_endless_pending_gives_up():
    message = voice(responses=[answer(pending=True)] * 20)

    assert await tg_asr.transcribe_message(message, max_wait_sec=10, sleep=no_sleep) is None
    assert message.client.calls < 6, "не долбим Telegram бесконечно"


async def test_message_without_client():
    message = voice()
    message.client = None

    assert await tg_asr.transcribe_message(message, sleep=no_sleep) is None


def test_duration_is_read_from_attributes():
    assert tg_asr.voice_duration(voice(duration=95)) == 95
    assert tg_asr.voice_duration(SimpleNamespace(document=None)) == 0


# ------------------------------------------------------ поведение очереди

async def test_queue_prefers_telegram_and_skips_download(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
):
    """Главная выгода: файл не качается, процессор не тратится."""
    from src.media import pipeline as mp
    from src.media.pipeline import MediaJob, MediaQueue

    saved: list[tuple[int, int, str]] = []

    async def set_transcript(chat_id: int, tg_msg_id: int, text: str) -> bool:
        saved.append((chat_id, tg_msg_id, text))
        return True

    async def must_not_run(*a: Any, **kw: Any) -> Any:
        raise AssertionError("ни скачивания, ни whisper быть не должно")

    monkeypatch.setattr(mp.repo, "set_transcript", set_transcript)
    monkeypatch.setattr(mp.repo, "set_media_path", must_not_run)

    async def telegram(message: Any) -> str:
        return "расшифровано телеграмом"

    class FakeMessage:
        async def download_media(self, file: str) -> str:
            raise AssertionError("файл качать не должны")

    queue = MediaQueue(
        must_not_run, telegram_transcriber=telegram, media_dir=tmp_path, concurrency=1
    )
    queue.submit(MediaJob(
        chat_id=3, chat_tg_id=-100, tg_msg_id=1, media_type="voice", message=FakeMessage()
    ))
    await queue.start()
    await queue._queue.join()
    await queue.stop()

    assert saved == [(3, 1, "расшифровано телеграмом")]
    assert queue.stats.by_telegram == 1
    assert queue.stats.by_whisper == 0


async def test_queue_falls_back_to_whisper(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    from src.media import pipeline as mp
    from src.media.pipeline import MediaJob, MediaQueue

    saved: list[str] = []

    async def set_transcript(chat_id: int, tg_msg_id: int, text: str) -> bool:
        saved.append(text)
        return True

    async def set_media_path(*a: Any, **kw: Any) -> bool:
        return True

    monkeypatch.setattr(mp.repo, "set_transcript", set_transcript)
    monkeypatch.setattr(mp.repo, "set_media_path", set_media_path)
    monkeypatch.setattr(mp.cfg, "ASR_PROVIDER", "auto")

    async def telegram(message: Any) -> None:
        return None            # Premium не помог

    async def whisper(path: Any) -> str:
        return "расшифровано локально"

    class FakeMessage:
        async def download_media(self, file: str) -> str:
            from pathlib import Path

            path = Path(f"{file}.ogg")
            path.write_bytes(b"audio")
            return str(path)

    queue = MediaQueue(
        whisper, telegram_transcriber=telegram, media_dir=tmp_path, concurrency=1
    )
    queue.submit(MediaJob(
        chat_id=3, chat_tg_id=-100, tg_msg_id=1, media_type="voice", message=FakeMessage()
    ))
    await queue.start()
    await queue._queue.join()
    await queue.stop()

    assert saved == ["расшифровано локально"]
    assert queue.stats.by_whisper == 1


async def test_telegram_only_mode_does_not_use_whisper(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
):
    """ASR_PROVIDER=telegram — значит whisper в образе может и не стоять."""
    from src.media import pipeline as mp
    from src.media.pipeline import MediaJob, MediaQueue

    monkeypatch.setattr(mp.cfg, "ASR_PROVIDER", "telegram")

    async def telegram(message: Any) -> None:
        return None

    async def must_not_run(*a: Any, **kw: Any) -> Any:
        raise AssertionError("whisper вызываться не должен")

    queue = MediaQueue(
        must_not_run, telegram_transcriber=telegram, media_dir=tmp_path, concurrency=1
    )
    queue.submit(MediaJob(
        chat_id=3, chat_tg_id=-100, tg_msg_id=1, media_type="voice", message=object()
    ))
    await queue.start()
    await queue._queue.join()
    await queue.stop()

    assert queue.stats.failed == 0, "отсутствие расшифровки — не ошибка"
