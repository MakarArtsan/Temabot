"""Короткое описание картинок мультимодальной моделью (TZ §4.1).

Описание кладётся в `transcript` — туда же, куда расшифровка голосового. Так
поиск и дайджест видят картинку как текст, не зная, что это была картинка.

Выключено по умолчанию: на активной группе это сотни вызовов модели в день.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path

from src.config import cfg
from src.llm.client import chat

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 4 * 1024 * 1024   # больше провайдеры обычно и не принимают

VISION_SYSTEM = """\
Ты описываешь картинку из рабочего чата одним-двумя предложениями по-русски.

Назови, что на ней: скриншот интерфейса, график, мем, фотография, схема.
Если есть читаемый текст, цифры, цены или названия — приведи их: именно за ними
потом будут искать. Не выдумывай того, чего не видно.
"""


def encode_image(path: str | Path) -> tuple[str, str] | None:
    """Файл -> (data-URL, тип). None, если файл великоват или не картинка."""
    file = Path(path)
    if not file.exists():
        return None

    size = file.stat().st_size
    if size > MAX_IMAGE_BYTES:
        log.info("Картинка %s весит %s байт — пропускаю", file.name, size)
        return None

    media_type = mimetypes.guess_type(file.name)[0] or "image/jpeg"
    if not media_type.startswith("image/"):
        return None

    encoded = base64.b64encode(file.read_bytes()).decode()
    return f"data:{media_type};base64,{encoded}", media_type


async def describe_image(path: str | Path, *, chat_id: int | None = None) -> str:
    """Описание картинки. Пустая строка, если не получилось."""
    if not cfg.VISION_ENABLED:
        return ""

    encoded = encode_image(path)
    if encoded is None:
        return ""

    data_url, _ = encoded
    try:
        reply = await chat(
            [
                {"role": "system", "content": VISION_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Что на этой картинке?"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            purpose="vision",
            chat_id=chat_id,
            max_tokens=300,
            attempts=2,
        )
    except Exception:
        # картинка без описания — это не повод терять сообщение
        log.warning("Не удалось описать картинку %s", path, exc_info=True)
        return ""

    return reply.text.strip()
