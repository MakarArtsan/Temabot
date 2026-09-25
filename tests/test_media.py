"""Тесты очереди расшифровки (TZ шаг 5). Настоящий whisper здесь не нужен."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from src.collector.service import Collector
from src.media import pipeline as mp
from src.media.pipeline import MediaJob, MediaQueue, NullMediaQueue


class FakeMessage:
    """Telethon-сообщение: умеет только отдавать файл."""

    def __init__(self, *, fail: bool = False, no_file: bool = False) -> None:
        self.fail = fail
        self.no_file = no_file
        self.downloads = 0

    async def download_media(self, file: str) -> str | None:
        self.downloads += 1
        if self.fail:
            raise ConnectionError("Telegram отвалился")
        if self.no_file:
            return None  # Telegram не отдал файл
        path = Path(f"{file}.ogg")
        path.write_bytes(b"fake audio")
        return str(path)


def job(tmp_path: Path, msg: FakeMessage, *, msg_id: int = 1, media: str = "voice") -> MediaJob:
    return MediaJob(
        chat_id=3, chat_tg_id=-100123, tg_msg_id=msg_id, media_type=media, message=msg
    )


@pytest.fixture
def db_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Подменяем repo: проверяем логику очереди, а не SQL."""
    calls: dict[str, list[Any]] = {"transcript": [], "path": []}

    async def set_transcript(chat_id: int, tg_msg_id: int, text: str) -> bool:
        calls["transcript"].append((chat_id, tg_msg_id, text))
        return True

    async def set_media_path(chat_id: int, tg_msg_id: int, path: str) -> bool:
        calls["path"].append((chat_id, tg_msg_id, path))
        return True

    monkeypatch.setattr(mp.repo, "set_transcript", set_transcript)
    monkeypatch.setattr(mp.repo, "set_media_path", set_media_path)
    return calls


async def _drain(queue: MediaQueue) -> None:
    await queue.start()
    await queue._queue.join()
    await queue.stop()


# ------------------------------------------------------------- базовый путь

async def test_voice_is_downloaded_and_transcribed(tmp_path: Path, db_calls):
    async def transcriber(path: str | Path) -> str:
        return "привет из голосового"

    msg = FakeMessage()
    queue = MediaQueue(transcriber, media_dir=tmp_path, concurrency=1)
    queue.submit(job(tmp_path, msg))
    await _drain(queue)

    assert msg.downloads == 1
    assert db_calls["transcript"] == [(3, 1, "привет из голосового")]
    assert db_calls["path"][0][2].endswith("1.ogg")
    assert queue.stats.done == 1


async def test_empty_transcript_is_not_written(tmp_path: Path, db_calls):
    """Тишина или музыка — писать в базу пустую строку незачем."""
    async def transcriber(path: str | Path) -> str:
        return "   "

    queue = MediaQueue(transcriber, media_dir=tmp_path, concurrency=1)
    queue.submit(job(tmp_path, FakeMessage()))
    await _drain(queue)

    assert db_calls["transcript"] == []
    assert queue.stats.done == 1, "это не ошибка — просто речи не нашлось"


# ------------------------------------------------------- что берём в работу

@pytest.mark.parametrize(
    ("media_type", "accepted"),
    [("voice", True), ("audio", True), ("photo", False), ("video", False), ("doc", False)],
)
def test_only_audio_goes_to_transcription(tmp_path: Path, media_type: str, accepted: bool):
    queue = MediaQueue(lambda p: asyncio.sleep(0, ""), media_dir=tmp_path)
    assert queue.submit(job(tmp_path, FakeMessage(), media=media_type)) is accepted


def test_full_queue_drops_instead_of_blocking(tmp_path: Path):
    """Потерять расшифровку не страшно. Заблокировать приём сообщений — страшно."""
    queue = MediaQueue(lambda p: asyncio.sleep(0, ""), media_dir=tmp_path, maxsize=2)

    accepted = [queue.submit(job(tmp_path, FakeMessage(), msg_id=n)) for n in range(5)]

    assert accepted == [True, True, False, False, False]
    assert queue.stats.queued == 2
    assert queue.stats.dropped == 3


# ------------------------------------------------------------- устойчивость

async def test_failed_download_does_not_stop_the_queue(tmp_path: Path, db_calls):
    async def transcriber(path: str | Path) -> str:
        return "второе сообщение"

    queue = MediaQueue(transcriber, media_dir=tmp_path, concurrency=1)
    queue.submit(job(tmp_path, FakeMessage(fail=True), msg_id=1))
    queue.submit(job(tmp_path, FakeMessage(), msg_id=2))
    await _drain(queue)

    assert queue.stats.failed == 1
    assert queue.stats.done == 1
    assert db_calls["transcript"] == [(3, 2, "второе сообщение")], "второе прошло"


async def test_transcriber_error_is_survived(tmp_path: Path, db_calls):
    async def transcriber(path: str | Path) -> str:
        raise RuntimeError("модель не загрузилась")

    queue = MediaQueue(transcriber, media_dir=tmp_path, concurrency=1)
    queue.submit(job(tmp_path, FakeMessage()))
    await _drain(queue)

    assert queue.stats.failed == 1
    assert db_calls["transcript"] == []


async def test_missing_file_is_an_error_not_a_crash(tmp_path: Path, db_calls):
    queue = MediaQueue(lambda p: asyncio.sleep(0, ""), media_dir=tmp_path, concurrency=1)
    queue.submit(job(tmp_path, FakeMessage(no_file=True)))
    await _drain(queue)

    assert queue.stats.failed == 1


# ------------------------------------------------------------- хранение файлов

async def test_file_is_removed_when_keep_files_is_off(tmp_path: Path, db_calls):
    async def transcriber(path: str | Path) -> str:
        assert Path(path).exists(), "файл нужен до расшифровки"
        return "текст"

    queue = MediaQueue(transcriber, media_dir=tmp_path, concurrency=1, keep_files=False)
    queue.submit(job(tmp_path, FakeMessage()))
    await _drain(queue)

    assert list(tmp_path.rglob("*.ogg")) == [], "после расшифровки файл не нужен"


async def test_file_is_kept_by_default(tmp_path: Path, db_calls):
    queue = MediaQueue(
        lambda p: asyncio.sleep(0, "текст"), media_dir=tmp_path, concurrency=1, keep_files=True
    )
    queue.submit(job(tmp_path, FakeMessage()))
    await _drain(queue)

    assert len(list(tmp_path.rglob("*.ogg"))) == 1


# --------------------------------------------------------------- параллельность

async def test_no_more_than_two_at_once(tmp_path: Path, db_calls):
    """ТЗ: не больше двух расшифровок одновременно."""
    running = 0
    peak = 0

    async def transcriber(path: str | Path) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "текст"

    queue = MediaQueue(transcriber, media_dir=tmp_path, concurrency=2)
    for n in range(6):
        queue.submit(job(tmp_path, FakeMessage(), msg_id=n))
    await _drain(queue)

    assert peak == 2
    assert queue.stats.done == 6


async def test_receiving_messages_is_not_blocked_by_transcription(tmp_path: Path, db_calls):
    """Главное ради чего всё затевалось: приём не ждёт расшифровку."""
    started = asyncio.Event()

    async def slow_transcriber(path: str | Path) -> str:
        started.set()
        await asyncio.sleep(5)  # «трёхминутное голосовое»
        return "поздно"

    queue = MediaQueue(slow_transcriber, media_dir=tmp_path, concurrency=1)
    await queue.start()

    collector = Collector(object(), dry_run=True, media=queue)
    queue.submit(job(tmp_path, FakeMessage()))
    await asyncio.wait_for(started.wait(), timeout=1)

    # пока идёт расшифровка, коллектор продолжает принимать сообщения
    await asyncio.wait_for(
        collector.on_new_message(_event(-100999)), timeout=0.5
    )
    await queue.stop()
    assert collector.saved == 0  # чужой чат, но главное — не зависли


def _event(chat_id: int) -> Any:
    from types import SimpleNamespace

    from tests.test_collector import fake_message

    return SimpleNamespace(chat_id=chat_id, message=fake_message(), id=1)


# ----------------------------------------------------------------- заглушка

async def test_null_queue_accepts_nothing(tmp_path: Path):
    queue = NullMediaQueue()
    assert queue.submit(job(tmp_path, FakeMessage())) is False
    assert queue.stats.skipped == 1
    await queue.start()
    await queue.stop()


def test_stats_shape_for_admin_page():
    stats = mp.QueueStats(
        queued=5, done=3, failed=1, dropped=1, skipped=2, by_telegram=2, by_whisper=1
    )
    assert stats.as_dict() == {
        "queued": 5, "done": 3, "failed": 1, "dropped": 1, "skipped": 2,
        "by_telegram": 2, "by_whisper": 1,
    }
