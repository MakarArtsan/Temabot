"""Работа не теряется: кэш ответов модели и продолжение задач после перезапуска."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.config import cfg
from src.db import repo
from src.db.models import Chat
from src.llm import client as llm
from src.web import app as web_app
from src.web import auth, jobs

OWNER = 132036441
DAY = date(2026, 9, 24)


# ================================================================ кэш ответов

class FakeCompletions:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls = 0

    async def create(self, **kw: Any) -> Any:
        self.calls += 1
        text = self.replies[min(self.calls, len(self.replies)) - 1]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20), model="m",
        )


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch, dsn: str) -> FakeCompletions:
    completions = FakeCompletions(['{"title": "тема"}'])
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(llm.cfg, "DATABASE_URL", dsn)
    return completions


MESSAGES = [{"role": "user", "content": "разбери тред"}]


async def test_same_request_is_paid_once(db: None, fake_llm: FakeCompletions):
    first, usage1 = await llm.chat_json(MESSAGES, purpose="summary", chat_id=1)
    second, usage2 = await llm.chat_json(MESSAGES, purpose="summary", chat_id=1)

    assert first == second == {"title": "тема"}
    assert fake_llm.calls == 1
    assert usage1.tokens_in == 100 and usage2.tokens_in == 0   # из кэша — бесплатно
    logged = await repo.pool.fetchval("select count(*) from llm_usage")
    assert logged == 1


async def test_changed_prompt_goes_to_the_model(db: None, fake_llm: FakeCompletions):
    await llm.chat_json(MESSAGES, purpose="summary")
    await llm.chat_json([{"role": "user", "content": "другой тред"}], purpose="summary")
    assert fake_llm.calls == 2


async def test_questions_to_the_bot_are_not_cached(db: None, fake_llm: FakeCompletions):
    await llm.chat_json(MESSAGES, purpose="qa")
    await llm.chat_json(MESSAGES, purpose="qa")
    assert fake_llm.calls == 2


async def test_broken_answer_is_not_cached(db: None, fake_llm: FakeCompletions):
    fake_llm.replies = ["не json вовсе", '{"title": "тема"}']
    with pytest.raises(ValueError):
        await llm.chat_json(MESSAGES, purpose="score")
    data, _ = await llm.chat_json(MESSAGES, purpose="score")
    assert data == {"title": "тема"} and fake_llm.calls == 2


async def test_old_cache_is_pruned(db: None, fake_llm: FakeCompletions):
    await llm.chat_json(MESSAGES, purpose="summary")
    await repo.pool.execute("update llm_cache set created_at = now() - interval '30 days'")
    assert await repo.prune_llm_cache(llm.CACHE_DAYS) == 1
    await llm.chat_json(MESSAGES, purpose="summary")
    assert fake_llm.calls == 2


async def test_cache_failure_does_not_block_the_model(monkeypatch: pytest.MonkeyPatch):
    completions = FakeCompletions(['{"ok": 1}'])
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(llm.cfg, "DATABASE_URL", "postgresql://x@127.0.0.1:1/none")

    async def broken(*a: Any, **kw: Any) -> Any:
        raise ConnectionError("база недоступна")

    monkeypatch.setattr(llm.repo, "get_llm_cache", broken)
    monkeypatch.setattr(llm.repo, "put_llm_cache", broken)
    monkeypatch.setattr(llm.repo, "log_llm_usage", broken)
    data, _ = await llm.chat_json(MESSAGES, purpose="summary")
    assert data == {"ok": 1}


# ============================================================= задачи в базе

@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Таблица state в памяти."""
    store: dict[str, Any] = {}

    async def set_state(key: str, value: Any) -> None:
        store[key] = value

    async def delete_state(key: str) -> None:
        store.pop(key, None)

    async def get_states(prefix: str = "") -> dict[str, Any]:
        return {k: v for k, v in store.items() if k.startswith(prefix)}

    monkeypatch.setattr(cfg, "DATABASE_URL", "postgresql://x@127.0.0.1:1/none")
    monkeypatch.setattr(web_app.cfg, "DATABASE_URL", "postgresql://x@127.0.0.1:1/none")
    monkeypatch.setattr(jobs.cfg, "DATABASE_URL", "postgresql://x@127.0.0.1:1/none")
    for name, fn in {"set_state": set_state, "delete_state": delete_state,
                     "get_states": get_states}.items():
        monkeypatch.setattr(repo, name, fn)
        monkeypatch.setattr(jobs.repo, name, fn)
    jobs.clear()
    return store


def test_job_id_is_stable_and_url_safe():
    assert jobs.job_id("preview:1:2026-09-24") == "preview-1-2026-09-24"


async def test_running_job_is_remembered_until_it_finishes(state: dict[str, Any]):
    gate = asyncio.Event()

    async def action() -> int:
        await gate.wait()
        return 1

    job = jobs.start("digest:1:2026-09-24", "дайджест", action,
                     spec={"kind": "digest", "chat_id": 1, "day": "2026-09-24"})
    await asyncio.sleep(0)
    assert state["job:digest:1:2026-09-24"]["kind"] == "digest"

    gate.set()
    assert job.task is not None
    await job.task
    assert state == {}


async def test_stopped_admin_keeps_the_job_for_resume(state: dict[str, Any]):
    async def forever() -> None:
        await asyncio.Event().wait()

    job = jobs.start("preview:1:2026-09-24", "прогон", forever,
                     spec={"kind": "preview", "chat_id": 1, "day": "2026-09-24"})
    await asyncio.sleep(0)
    assert job.task is not None
    job.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job.task
    assert "job:preview:1:2026-09-24" in state


async def test_failed_job_is_not_resumed(state: dict[str, Any]):
    async def broken() -> None:
        raise RuntimeError("упало")

    job = jobs.start("digest:1:2026-09-24", "дайджест", broken,
                     spec={"kind": "digest", "chat_id": 1, "day": "2026-09-24"})
    assert job.task is not None
    await job.task
    assert job.error == "упало" and state == {}


async def test_interrupted_jobs_resume_after_restart(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
):
    chat = Chat(id=1, tg_id=-100111, title="Нейросети")
    state["job:preview:1:2026-09-24"] = {"kind": "preview", "chat_id": 1, "day": "2026-09-24"}
    state["job:digest:1:2026-09-23"] = {"kind": "digest", "chat_id": 1, "day": "2026-09-23",
                                        "resumed": jobs.MAX_RESUMES}   # роняет процесс
    state["job:digest:9:2026-09-23"] = {"kind": "digest", "chat_id": 9, "day": "2026-09-23"}
    launched: list[Any] = []

    async def get_chat_by_id(chat_id: int) -> Chat | None:
        return chat if chat_id == 1 else None

    async def prune(days: int) -> int:
        return 0

    def launch(kind: str) -> Any:
        def run(chat: Chat, day: date, *, resumed: int = 0) -> None:
            launched.append((kind, chat.id, day, resumed))
        return run

    monkeypatch.setattr(web_app.repo, "get_chat_by_id", get_chat_by_id)
    monkeypatch.setattr(web_app.repo, "prune_llm_cache", prune)
    monkeypatch.setattr(web_app, "LAUNCHERS", {"preview": launch("preview"),
                                                "digest": launch("digest")})
    await web_app.resume_jobs()

    assert launched == [("preview", 1, DAY, 1)]
    assert list(state) == ["job:preview:1:2026-09-24"]   # безнадёжные убраны


async def test_resumed_preview_keeps_its_address(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
):
    """Страница опрашивала preview-1-2026-09-24 — после перезапуска там же и результат."""
    from src.digest import pipeline as dp

    chat = Chat(id=1, tg_id=-100111, title="Нейросети")

    async def get_digest(*a: Any) -> None:
        return None

    async def build_digest(*a: Any, **kw: Any) -> Any:
        return SimpleNamespace(usage=llm.Usage(), all_topics=[], markdown="", llm_is_down=False)

    async def save_result(*a: Any, **kw: Any) -> int:
        return 77

    monkeypatch.setattr(web_app.repo, "get_digest", get_digest)
    monkeypatch.setattr(dp, "build_digest", build_digest)
    monkeypatch.setattr(dp, "save_result", save_result)

    job = web_app.launch_preview(chat, DAY, resumed=1)
    assert job.id == "preview-1-2026-09-24" and job.resumed == 1
    assert job.task is not None
    await job.task
    assert job.result["saved_id"] == 77 and state == {}


def test_page_waits_while_the_job_is_being_resumed(
    state: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
):
    for target in {id(cfg): cfg, id(auth.cfg): auth.cfg}.values():
        monkeypatch.setattr(target, "WEB_SECRET_KEY", "тестовый-секрет-достаточной-длины")
        monkeypatch.setattr(target, "OWNER_ID", OWNER)
    state["job:preview:1:2026-09-24"] = {"kind": "preview", "chat_id": 1, "day": "2026-09-24"}
    client = TestClient(web_app.app, base_url="https://testserver", follow_redirects=False)
    client.cookies.set(auth.COOKIE_NAME, auth.issue_session(OWNER))

    waiting = client.get("/jobs/preview-1-2026-09-24?view=preview")
    assert "продолжу" in waiting.text
    assert 'hx-get="/jobs/preview-1-2026-09-24?view=preview"' in waiting.text

    lost = client.get("/jobs/preview-1-2026-09-25?view=<script>")
    assert "не найдена" in lost.text and "<script>" not in lost.text


# ======================================================= эмбеддинги не настроены

async def test_missing_embeddings_are_reported_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    """Без sentence-transformers — одна строка в логе, а не трейсбек на каждую тему."""
    from src.nlp import embed
    from src.scoring import novelty

    loads = []

    def missing() -> None:
        loads.append(1)
        raise embed.EmbeddingsUnavailable("Не установлен sentence-transformers")

    monkeypatch.setattr(embed.cfg, "EMBED_BACKEND", "local")
    monkeypatch.setattr(embed, "_model", None)
    monkeypatch.setattr(embed, "_unavailable", "")
    monkeypatch.setattr(embed, "load_local_model", missing)

    with caplog.at_level("WARNING"):
        for n in range(5):
            assert await novelty.score_novelty(f"тема {n}", "вывод", [[1.0]]) == (0.8, None, 0.0)
            assert await novelty.find_similar(f"тема {n}", [{"embedding": [1.0]}]) == (None, 0.0)

    assert loads == [1]
    assert len(caplog.records) == 1 and "Эмбеддинги выключены" in caplog.records[0].message
    assert not any(r.exc_info for r in caplog.records)
