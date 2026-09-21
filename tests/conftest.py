"""Общие фикстуры. Интеграционные тесты идут на настоящем Postgres с pgvector.

DSN берётся из TEST_DATABASE_URL; без него тесты помечаются skip, чтобы
`make test` не падал на машине без базы.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

TEST_DSN = os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture(scope="session")
def dsn() -> str:
    if not TEST_DSN:
        pytest.skip("TEST_DATABASE_URL не задан — интеграционные тесты пропущены")
    return TEST_DSN


@pytest_asyncio.fixture
async def db(dsn: str) -> AsyncIterator[None]:
    """Чистая база на каждый тест: применяем схему и чистим таблицы."""
    from src.db import pool
    from src.db.migrate import apply_schema

    await pool.close_pool()
    await apply_schema(dsn)
    await pool.execute(
        """
        truncate messages, chunks, digest_items, digests, feedback, qa_log,
                 thread_contrib, author_stats_daily, llm_usage, chats, authors,
                 copier_blocklist, state, settings
        restart identity cascade
        """
    )
    try:
        yield
    finally:
        await pool.close_pool()
