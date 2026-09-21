"""Тесты описания картинок (TZ §4.1)."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from src.config import cfg
from src.media import image as vision


@pytest.fixture(autouse=True)
def vision_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "VISION_ENABLED", True)


def a_png(tmp_path: Path, size: int = 100) -> Path:
    file = tmp_path / "shot.png"
    file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * size)
    return file


def test_encoding_produces_data_url(tmp_path: Path):
    url, media_type = vision.encode_image(a_png(tmp_path))

    assert url.startswith("data:image/png;base64,")
    assert media_type == "image/png"
    assert base64.b64decode(url.split(",", 1)[1]).startswith(b"\x89PNG")


def test_huge_images_are_skipped(tmp_path: Path):
    """Провайдеры всё равно не примут, а трафик и деньги потратятся."""
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG" + b"x" * (vision.MAX_IMAGE_BYTES + 1))

    assert vision.encode_image(big) is None


def test_non_images_are_skipped(tmp_path: Path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4")

    assert vision.encode_image(doc) is None


def test_missing_file_is_skipped(tmp_path: Path):
    assert vision.encode_image(tmp_path / "нет-такого.png") is None


async def test_description_goes_to_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.llm.client import LLMReply, Usage

    calls: list[dict[str, Any]] = []

    async def fake_chat(messages: list[dict[str, Any]], **kw: Any) -> LLMReply:
        calls.append({"messages": messages, **kw})
        return LLMReply(text="  Скриншот с ценой 12 рублей  ", usage=Usage())

    monkeypatch.setattr(vision, "chat", fake_chat)

    text = await vision.describe_image(a_png(tmp_path), chat_id=7)

    assert text == "Скриншот с ценой 12 рублей"
    assert calls[0]["purpose"] == "vision", "расход виден отдельной строкой"
    assert calls[0]["chat_id"] == 7
    blocks = calls[0]["messages"][1]["content"]
    assert any(b["type"] == "image_url" for b in blocks)


async def test_disabled_vision_costs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "VISION_ENABLED", False)

    async def must_not_run(*a: Any, **kw: Any) -> Any:
        raise AssertionError("модель не должна вызываться")

    monkeypatch.setattr(vision, "chat", must_not_run)

    assert await vision.describe_image(a_png(tmp_path)) == ""


async def test_model_failure_does_not_lose_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    async def broken(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(vision, "chat", broken)

    assert await vision.describe_image(a_png(tmp_path)) == ""


async def test_queue_routes_photos_to_the_describer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Картинка должна попасть описателю, а не расшифровщику."""
    from src.media import pipeline as mp
    from src.media.pipeline import MediaJob, MediaQueue

    monkeypatch.setattr(mp.cfg, "VISION_ENABLED", True)
    described: list[str] = []
    saved: list[tuple[int, int, str]] = []

    async def transcriber(path: Any) -> str:
        raise AssertionError("для картинки расшифровщик не нужен")

    async def describer(path: Any, **kw: Any) -> str:
        described.append(str(path))
        return "скриншот интерфейса"

    async def set_transcript(chat_id: int, tg_msg_id: int, text: str) -> bool:
        saved.append((chat_id, tg_msg_id, text))
        return True

    async def set_media_path(*a: Any, **kw: Any) -> bool:
        return True

    monkeypatch.setattr(mp.repo, "set_transcript", set_transcript)
    monkeypatch.setattr(mp.repo, "set_media_path", set_media_path)

    class FakeMessage:
        async def download_media(self, file: str) -> str:
            path = Path(f"{file}.jpg")
            path.write_bytes(b"jpeg")
            return str(path)

    queue = MediaQueue(transcriber, describer=describer, media_dir=tmp_path, concurrency=1)
    assert queue.submit(MediaJob(
        chat_id=3, chat_tg_id=-100, tg_msg_id=1, media_type="photo", message=FakeMessage()
    )) is True

    await queue.start()
    await queue._queue.join()
    await queue.stop()

    assert described, "описатель вызван"
    assert saved and saved[0][2] == "скриншот интерфейса"


def test_photos_are_ignored_when_vision_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.media import pipeline as mp
    from src.media.pipeline import MediaJob, MediaQueue

    monkeypatch.setattr(mp.cfg, "VISION_ENABLED", False)
    queue = MediaQueue(lambda p: None, media_dir=tmp_path)  # type: ignore[arg-type]

    accepted = queue.submit(MediaJob(
        chat_id=3, chat_tg_id=-100, tg_msg_id=1, media_type="photo", message=object()
    ))
    assert accepted is False
