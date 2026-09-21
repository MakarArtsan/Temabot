"""Единственная точка входа к LLM. Модель и провайдер меняются через .env.

TZ §1: любой OpenAI-совместимый провайдер (DeepSeek / GLM / OpenRouter).
"""
from __future__ import annotations

# TODO(шаг 6): AsyncOpenAI-клиент, chat_json(), учёт токенов в llm_usage.
