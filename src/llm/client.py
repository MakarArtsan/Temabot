"""Единственная точка входа к LLM (CLAUDE.md, TZ §1).

Провайдер любой OpenAI-совместимый: DeepSeek, GLM, OpenRouter. Модель и base_url
берутся из .env и нигде не хардкодятся — смена провайдера не трогает код.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from src.config import cfg
from src.db import repo

log = logging.getLogger(__name__)

_client: Any = None

# ```json ... ``` — модели любят заворачивать ответ в разметку, даже когда просишь не надо
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass(slots=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            model=self.model or other.model,
        )


@dataclass(slots=True)
class LLMReply:
    text: str
    usage: Usage


def get_client() -> Any:
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        if not cfg.LLM_API_KEY:
            raise RuntimeError("Не задан LLM_API_KEY — см. docs/SETUP.md")
        _client = AsyncOpenAI(
            api_key=cfg.LLM_API_KEY, base_url=cfg.LLM_BASE_URL, timeout=120.0, max_retries=0
        )
    return _client


def reset_client() -> None:
    """Для тестов и смены модели на лету."""
    global _client
    _client = None


def extract_json(text: str) -> Any:
    """Достать JSON из ответа модели.

    Модель может обернуть ответ в ```json, добавить «Вот результат:» или выдать
    чистый JSON. Разбираем все три случая, иначе на каждом дайджесте будут
    случайные падения.
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Пустой ответ модели")

    fenced = _FENCE_RE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # последняя попытка: вырезать всё от первой скобки до парной последней
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Не удалось разобрать JSON из ответа модели: {raw[:200]!r}")


async def chat(
    messages: list[dict[str, str]],
    *,
    purpose: str,
    chat_id: int | None = None,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    attempts: int = 3,
    json_mode: bool = False,
) -> LLMReply:
    """Запрос к модели с повторами и учётом токенов в llm_usage."""
    client = get_client()
    extra_body = dict(cfg.llm_extra_body)
    kwargs: dict[str, Any] = {
        "model": cfg.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if extra_body:
        kwargs["extra_body"] = extra_body

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.chat.completions.create(**kwargs)
            break
        except Exception as exc:
            last_error = exc
            delay = 2.0 * (2**attempt)
            log.warning(
                "LLM не ответила (%s), попытка %s/%s, пауза %.0f сек",
                exc, attempt + 1, attempts, delay,
            )
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
    else:  # pragma: no cover — защита от изменения логики цикла
        raise last_error or RuntimeError("LLM недоступна")

    usage = _read_usage(response)
    await _log_usage(usage, purpose=purpose, chat_id=chat_id)
    return LLMReply(text=_read_text(response), usage=usage)


async def chat_json(
    messages: list[dict[str, str]],
    *,
    purpose: str,
    chat_id: int | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> tuple[Any, Usage]:
    """То же, но ответ разбирается как JSON. Строгий режим включён там, где есть."""
    reply = await chat(
        messages,
        purpose=purpose,
        chat_id=chat_id,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=True,
    )
    return extract_json(reply.text), reply.usage


def _read_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return getattr(choices[0].message, "content", "") or ""


def _read_usage(response: Any) -> Usage:
    usage = getattr(response, "usage", None)
    return Usage(
        tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
        tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
        model=getattr(response, "model", None) or cfg.LLM_MODEL,
    )


async def _log_usage(usage: Usage, *, purpose: str, chat_id: int | None) -> None:
    """Расход токенов нужен для дашборда (§4.9). Сбой записи не ломает ответ."""
    try:
        await repo.log_llm_usage(
            chat_id=chat_id,
            purpose=purpose,
            model=usage.model,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
        )
    except Exception:
        log.warning("Не удалось записать расход токенов", exc_info=True)
