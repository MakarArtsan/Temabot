"""Веб-админка: FastAPI + Jinja2 + HTMX + Chart.js (TZ §4.9).

Публичны только вход и проверка живости. Админка закрыта зависимостью
`require_owner`: в ней лежит содержимое закрытых групп (TZ §9). Страница
участников (`/g`, src/web/portal.py) — только чтение и только для тех, кто
состоит в группе, где её открыл владелец.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date as date_type
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import cfg
from src.db import pool, repo
from src.digest.publish import (
    PUBLISH_LABELS,
    PUBLISH_MODES,
    PublishResult,
    group_preview,
    publish_digest,
    unpublish_digest,
)
from src.digest.render import deeplink
from src.web import auth, labels, membership

log = logging.getLogger(__name__)

HEALTHZ_DB_TIMEOUT_SEC = 5.0
WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Пул открывается лениво, при первом запросе к базе.

    Если ждать базу на старте, недоступная база не даёт uvicorn открыть порт,
    и хостинг считает запуск проваленным, хотя /healthz мог бы честно ответить.
    """
    yield
    await pool.close_pool()


# docs_url и openapi_url отключены: схема API — это тоже информация о системе
app = FastAPI(
    title="tg-digest admin",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
# стили, скрипты и библиотеки лежат в репозитории: страница не зависит от CDN
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
    """Страницы не встраиваются в чужие сайты и не отдают адрес наружу по ссылкам."""
    response: Response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/static/"):
        # в админке и на странице участников — содержимое закрытой группы
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _asset_version() -> str:
    """Меняется вместе со стилями и скриптами — браузер не держит старые."""
    digest = hashlib.sha256()
    for name in ("app.css", "app.js"):
        path = STATIC_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _local_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(ZoneInfo(cfg.TZ)).strftime("%d.%m %H:%M")


def chat_name(chat: Any) -> str:
    """Название группы; пока оно неизвестно — понятная замена вместо голого id."""
    if chat is None:
        return "Группа"
    get = chat.get if isinstance(chat, dict) else lambda key: getattr(chat, key, None)
    title = (get("title") or "").strip()
    if title:
        return title
    tg_id = get("tg_id")
    return f"Группа {tg_id}" if tg_id else "Группа"


def plural(n: Any, one: str, few: str, many: str) -> str:
    """1 тема, 2 темы, 5 тем."""
    try:
        value = abs(int(n))
    except (TypeError, ValueError):
        return many
    if 11 <= value % 100 <= 14:
        return many
    if value % 10 == 1:
        return one
    if 2 <= value % 10 <= 4:
        return few
    return many


def num(value: Any) -> str:
    """12 345 — с узким пробелом, как принято в русском тексте."""
    try:
        return f"{int(value):,}".replace(",", "\u202f")
    except (TypeError, ValueError):
        return str(value)


def money(value: Any) -> str:
    amount = float(value or 0)
    if amount == 0:
        return "$0"
    if amount < 0.01:
        return "< $0.01"
    return f"${amount:,.2f}".replace(",", "\u202f")


WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS_SHORT = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def cost_of(tokens_in: Any, tokens_out: Any) -> float:
    """Оценка расходов на модель по ценам из настроек (за миллион токенов)."""
    return (
        float(tokens_in or 0) / 1e6 * cfg.LLM_PRICE_IN
        + float(tokens_out or 0) / 1e6 * cfg.LLM_PRICE_OUT
    )


TEMPLATES.env.globals.update(
    deeplink=deeplink,
    publish_labels=PUBLISH_LABELS,
    portal_labels=labels.PORTAL,
    copier_labels=labels.COPIER,
    feature_labels=labels.FEATURES,
    weight_settings=labels.WEIGHTS,
    penalty_settings=labels.PENALTIES,
    kind_label=lambda kind: labels.KINDS.get(str(kind or "other"), labels.KINDS["other"]),
    chat_name=chat_name,
    plural=plural,
    num=num,
    money=money,
    weekday=lambda day: WEEKDAYS[day.weekday()],
    month_short=lambda day: MONTHS_SHORT[day.month - 1],
    asset_version=_asset_version(),
)
TEMPLATES.env.filters["local_time"] = _local_time


def make_bot() -> Any:
    """Бот для публикации из админки. Отдельный от процесса бота — это обычный
    HTTP Bot API, отправке сообщений параллельный polling не мешает."""
    from src.bot.client import create_bot

    return create_bot()


async def with_bot(action: Callable[[Any], Awaitable[PublishResult]]) -> PublishResult:
    if not cfg.BOT_TOKEN:
        return PublishResult(False, "Не задан BOT_TOKEN — публиковать некем")
    try:
        bot = make_bot()
    except Exception as exc:  # например, токен неверного формата
        return PublishResult(False, f"Не удалось подключить бота: {exc}")
    try:
        return await action(bot)
    finally:
        await bot.session.close()


def portal_url() -> str:
    """Ссылка на страницу участников — её владелец отправляет в группу сам."""
    base = cfg.WEB_BASE_URL.rstrip("/")
    return f"{base}/g" if base else "/g"


def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    session = auth.current_user(request)
    context.setdefault("csrf", (session or {}).get("csrf", ""))
    context.setdefault("portal_url", portal_url())
    return TEMPLATES.TemplateResponse(request, name, context)


def local_today() -> date_type:
    return datetime.now(ZoneInfo(cfg.TZ)).date()


def _is_portal(path: str) -> bool:
    return path == "/g" or path.startswith("/g/")


@app.exception_handler(HTTPException)
async def on_http_error(request: Request, exc: HTTPException) -> Response:
    """Неавторизованного уводим на вход, а не показываем голую ошибку."""
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    if _is_portal(request.url.path) and exc.status_code in (403, 404):
        viewer = auth.current_viewer(request)
        return TEMPLATES.TemplateResponse(
            request,
            "portal/denied.html",
            {"is_owner": auth.session_uid(viewer) == cfg.OWNER_ID},
            status_code=404,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Проверка живости для Amvera и docker HEALTHCHECK.

    Единственная публичная ручка кроме страницы входа: она не отдаёт ничего о
    содержимом групп — только то, что процесс жив и база отвечает.
    """
    database = "off"
    if cfg.DATABASE_URL:
        try:
            # проверка живости не должна висеть минуту на недоступной базе
            await asyncio.wait_for(pool.fetchval("select 1"), HEALTHZ_DB_TIMEOUT_SEC)
            database = "ok"
        except Exception:
            database = "fail"
    return JSONResponse({"status": "ok", "database": database})


# ---------------------------------------------------------------------- вход

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    if auth.current_user(request) is not None:
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
    if auth.current_viewer(request) is not None and not error:
        # участник уже вошёл — ему сюда, а не в админку
        return RedirectResponse("/g", status_code=303)  # type: ignore[return-value]
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {
            "bot_username": cfg.BOT_USERNAME,
            "auth_url": f"{cfg.WEB_BASE_URL.rstrip('/')}/auth/telegram",
            "error": error,
        },
    )


@app.get("/auth/telegram")
async def telegram_callback(request: Request) -> Response:
    """Виджет возвращает подписанные данные пользователя (TZ §4.9)."""
    data = dict(request.query_params)
    try:
        user_id = auth.check_telegram_auth(data)
    except auth.AuthError as exc:
        log.warning("Неудачный вход в админку: %s", exc)
        return RedirectResponse(f"/login?error={exc}", status_code=303)

    target = "/"
    if user_id != cfg.OWNER_ID:
        # не владелец — может быть участником группы с открытой страницей
        try:
            allowed = await membership.visible_chats(user_id)
        except Exception:
            log.exception("Не удалось проверить доступ %s", user_id)
            return RedirectResponse(
                "/login?error=Сейчас не получается проверить доступ, попробуйте позже",
                status_code=303,
            )
        if not allowed:
            log.info("Вход без доступа: %s не состоит в открытых группах", user_id)
            return RedirectResponse(
                "/login?error=Страница открыта только участникам группы. "
                "Если вы только что вступили — попробуйте через пару минут.",
                status_code=303,
            )
        target = "/g"

    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(user_id),
        max_age=auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=cfg.WEB_BASE_URL.startswith("https"),
    )
    return response


@app.get("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


# ------------------------------------------------------------------ дашборд

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _: dict = Depends(auth.require_owner)) -> HTMLResponse:
    from src.web.portal import daily_series

    stats = await repo.get_collection_stats()
    per_day = await repo.messages_per_day(days=30)
    tokens = await repo.tokens_per_day(days=30)
    states = await repo.get_states()
    digests = await repo.list_recent_digests(limit=5)

    today = local_today()
    counts = {row["day"]: int(row["count"]) for row in per_day}
    costs = {t["day"]: round(cost_of(t["tokens_in"], t["tokens_out"]), 4) for t in tokens}

    now = datetime.now(ZoneInfo(cfg.TZ))
    return render(
        request,
        "dashboard.html",
        {
            "active": "dashboard",
            "stats": stats,
            "totals": {
                "messages": sum(counts.values()),
                "today": counts.get(today, 0),
                "cost": sum(costs.values()),
                "cost_today": costs.get(today, 0.0),
            },
            "activity": daily_series(counts, today, 30),
            "costs": daily_series(costs, today, 30, unit="usd", fmt=money),
            "price_note": f"${cfg.LLM_PRICE_IN:g} / ${cfg.LLM_PRICE_OUT:g} за миллион токенов",
            "pending_media": await repo.pending_media_count(),
            "processes": labels.describe_states(states, now),
            "digests": digests,
            "chats": await repo.list_chats(),
        },
    )


# ------------------------------------------------------------------- группы

@app.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, _: dict = Depends(auth.require_owner)) -> HTMLResponse:
    return render(
        request, "groups.html", {"active": "groups", "chats": await repo.list_chats()}
    )


@app.post("/groups/{chat_tg_id}", response_class=HTMLResponse)
async def groups_update(
    request: Request,
    chat_tg_id: int,
    field: str = Form(...),
    value: str = Form(...),
    csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> HTMLResponse:
    auth.check_csrf(request, csrf_token)

    if field == "collect":
        await repo.set_chat_flags(chat_tg_id, collect=value == "1")
    elif field == "digest":
        await repo.set_chat_flags(chat_tg_id, digest=value == "1")
    elif field == "copier":
        if value not in {"allow", "deny", "ask"}:
            raise HTTPException(400, "Недопустимый режим копировщика")
        await repo.set_chat_flags(chat_tg_id, copier=value)
    elif field == "publish":
        # дайджест в саму группу: только мне / по кнопке / автоматически
        if value not in PUBLISH_MODES:
            raise HTTPException(400, "Недопустимый режим публикации")
        await repo.set_chat_flags(chat_tg_id, publish=value)
    elif field == "portal":
        # страница участников: закрыта / дайджесты / дайджесты и рейтинги
        if value not in labels.PORTAL_MODES:
            raise HTTPException(400, "Недопустимый режим страницы участников")
        changed = await repo.set_chat_flags(chat_tg_id, portal=value)
        if changed is not None:
            membership.forget(changed.id)
    elif field == "ratings_publish":
        current = await repo.get_chat_by_tg_id(chat_tg_id)
        if current is None:
            raise HTTPException(404, "Группа не найдена")
        ratings = dict((current.settings or {}).get("ratings") or {})
        ratings["publish"] = value == "1"
        await repo.update_chat_settings(current.id, {"ratings": ratings})
    else:
        raise HTTPException(400, "Неизвестное поле")

    # бот и коллектор перечитают настройки сами, но свой кэш сбросим сразу
    from src.bot.middlewares import settings_cache

    settings_cache.forget(chat_tg_id)

    chat = await repo.get_chat_by_tg_id(chat_tg_id)
    return render(request, "_group_row.html", {"chat": chat})


# -------------------------------------------------------------------- отбор

@app.get("/selection", response_class=HTMLResponse)
async def selection_page(
    request: Request, chat: int | None = None, _: dict = Depends(auth.require_owner)
) -> HTMLResponse:
    from src.scoring.score import (
        DEFAULT_PENALTIES,
        DEFAULT_THRESHOLD,
        DEFAULT_TOP_N,
        DEFAULT_WEIGHTS,
    )

    chats = await repo.list_chats()
    current = next((c for c in chats if c.id == chat), chats[0] if chats else None)
    settings = dict((current.settings if current else None) or {})

    weights = dict(DEFAULT_WEIGHTS)
    weights.update(settings.get("weights") or {})
    penalties = dict(DEFAULT_PENALTIES)
    penalties.update(settings.get("penalties") or {})

    return render(
        request,
        "selection.html",
        {
            "active": "selection",
            "chats": chats,
            "chat": current,
            "profile": settings.get("interests_profile", ""),
            "weights": weights,
            "penalties": penalties,
            "threshold": settings.get("threshold", DEFAULT_THRESHOLD),
            "default_threshold": DEFAULT_THRESHOLD,
            "top_n": settings.get("top_n", DEFAULT_TOP_N),
            "yesterday": local_today() - timedelta(days=1),
        },
    )


@app.post("/selection/{chat_id}")
async def selection_save(
    request: Request, chat_id: int, csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> Response:
    auth.check_csrf(request, csrf_token)
    form = await request.form()

    patch: dict[str, Any] = {"interests_profile": str(form.get("interests_profile", "")).strip()}
    weights: dict[str, float] = {}
    penalties: dict[str, float] = {}
    try:
        for key, raw in form.items():
            if key.startswith("w_"):
                weights[key] = round(float(str(raw)), 3)
            elif key.startswith("penalty_"):
                penalties[key.removeprefix("penalty_")] = round(float(str(raw)), 3)
        patch["threshold"] = round(float(str(form.get("threshold", 0.45))), 3)
        patch["top_n"] = int(float(str(form.get("top_n", 6))))
    except ValueError:
        raise HTTPException(400, "Недопустимое число в настройках") from None
    patch["weights"] = weights
    patch["penalties"] = penalties

    await repo.update_chat_settings(chat_id, patch)
    return RedirectResponse(f"/selection?chat={chat_id}", status_code=303)


@app.post("/selection/{chat_id}/preview", response_class=HTMLResponse)
async def selection_preview(
    request: Request, chat_id: int, day: str = Form(""), csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> HTMLResponse:
    """«Прогнать на вчера»: превью с текущими настройками, без отправки (TZ §4.9)."""
    auth.check_csrf(request, csrf_token)
    from src.digest import pipeline as digest_pipeline

    chat = await repo.get_chat_by_id(chat_id)
    if chat is None:
        raise HTTPException(404, "Группа не найдена")

    try:
        target = date_type.fromisoformat(day) if day else local_today() - timedelta(days=1)
    except ValueError:
        return render(request, "_preview.html", {"error": "дата должна быть вида 2026-09-20",
                                                 "day": local_today()})
    stored = await repo.get_digest(chat.id, target)

    try:
        # save=False: превью ничего не перезаписывает и никуда не отправляется
        result = await digest_pipeline.build_digest(chat, target)
    except Exception as exc:
        log.exception("Превью дайджеста не собралось")
        return render(request, "_preview.html", {"error": str(exc), "day": target})

    return render(
        request,
        "_preview.html",
        {
            "day": target,
            "result": result,
            "cost": cost_of(result.usage.tokens_in, result.usage.tokens_out),
            "old_markdown": stored.summary_md if stored else "",
            "topics": sorted(result.all_topics, key=lambda t: -t.score),
        },
    )


# --------------------------------------------------------------- дайджесты

@app.get("/digests", response_class=HTMLResponse)
async def digests_page(request: Request, _: dict = Depends(auth.require_owner)) -> HTMLResponse:
    return render(
        request,
        "digests.html",
        {"active": "digests", "digests": await repo.list_recent_digests(limit=40)},
    )


@app.get("/digests/{digest_id}", response_class=HTMLResponse)
async def digest_detail(
    request: Request, digest_id: int, _: dict = Depends(auth.require_owner)
) -> HTMLResponse:
    digest = await repo.get_digest_by_id(digest_id)
    if digest is None:
        raise HTTPException(404, "Дайджест не найден")
    chat = await repo.get_chat_by_id(digest.chat_id)
    items = await repo.get_digest_items(digest_id)
    return render(
        request,
        "digest_detail.html",
        {
            "active": "digests",
            "digest": digest,
            "chat": chat,
            "preview": group_preview(digest.payload, chat) if chat else [],
            "items": items,
            "shown": [i for i in items if i["shown"]],
            "missed": [i for i in items if not i["shown"]],
        },
    )


async def _publish_box(
    request: Request, digest_id: int, result: PublishResult | None
) -> HTMLResponse:
    digest = await repo.get_digest_by_id(digest_id)
    if digest is None:
        raise HTTPException(404, "Дайджест не найден")
    chat = await repo.get_chat_by_id(digest.chat_id)
    return render(
        request, "_publish_box.html", {"digest": digest, "chat": chat, "result": result}
    )


@app.post("/digests/{digest_id}/publish", response_class=HTMLResponse)
async def digest_publish(
    request: Request, digest_id: int, csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> HTMLResponse:
    """Опубликовать дайджест в его группе — после просмотра владельцем."""
    auth.check_csrf(request, csrf_token)
    result = await with_bot(lambda bot: publish_digest(bot, digest_id))
    return await _publish_box(request, digest_id, result)


@app.post("/digests/{digest_id}/unpublish", response_class=HTMLResponse)
async def digest_unpublish(
    request: Request, digest_id: int, csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> HTMLResponse:
    auth.check_csrf(request, csrf_token)
    result = await with_bot(lambda bot: unpublish_digest(bot, digest_id))
    return await _publish_box(request, digest_id, result)


@app.post("/feedback/{item_id}")
async def web_feedback(
    request: Request, item_id: int, value: int = Form(...), csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> HTMLResponse:
    """Оценка темы прямо из админки — в том числе отсеянной (TZ §4.7)."""
    auth.check_csrf(request, csrf_token)
    if value not in {1, -1, -2}:
        raise HTTPException(400, "Недопустимая оценка")
    await repo.add_feedback(item_id, value)
    mark = {1: "👍", -1: "👎", -2: "🔕"}[value]
    return HTMLResponse(f'<span class="muted">{mark} учтено</span>')


# ---------------------------------------------------------------- обучение

@app.get("/learning", response_class=HTMLResponse)
async def learning_page(request: Request, _: dict = Depends(auth.require_owner)) -> HTMLResponse:
    from src.scoring.score import DEFAULT_WEIGHTS

    chats = await repo.list_chats()
    rows = []
    for chat in chats:
        settings = chat.settings or {}
        current = dict(DEFAULT_WEIGHTS)
        current.update(settings.get("weights") or {})
        rows.append(
            {
                "chat": chat,
                "count": await repo.count_feedback(chat.id),
                "current": current,
                "proposal": settings.get("weights_proposal"),
            }
        )
    return render(request, "learning.html", {"active": "learning", "rows": rows})


@app.post("/learning/{chat_id}/apply")
async def learning_apply(
    request: Request, chat_id: int, csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> Response:
    """Применить предложенные веса. Решение принимает человек (TZ §4.7)."""
    auth.check_csrf(request, csrf_token)
    chat = await repo.get_chat_by_id(chat_id)
    proposal = ((chat.settings if chat else None) or {}).get("weights_proposal") or {}
    weights = proposal.get("weights")
    if not weights:
        raise HTTPException(400, "Предложения нет")

    await repo.update_chat_settings(
        chat_id, {"weights": weights, "weights_applied_at": datetime.now().isoformat()}
    )
    return RedirectResponse("/learning", status_code=303)


@app.post("/learning/{chat_id}/retrain")
async def learning_retrain(
    request: Request, chat_id: int, csrf_token: str = Form(""),
    _: dict = Depends(auth.require_owner),
) -> Response:
    auth.check_csrf(request, csrf_token)
    from src.jobs.retrain import retrain_chat

    await retrain_chat(chat_id)
    return RedirectResponse("/learning", status_code=303)


# --------------------------------------------------------------- участники

@app.get("/authors", response_class=HTMLResponse)
async def authors_page(request: Request, _: dict = Depends(auth.require_owner)) -> HTMLResponse:
    return render(
        request, "authors.html", {"active": "authors", "authors": await repo.list_authors()}
    )


@app.post("/authors/{tg_user_id}", response_class=HTMLResponse)
async def authors_update(
    request: Request, tg_user_id: int, field: str = Form(...), value: str = Form(...),
    csrf_token: str = Form(""), _: dict = Depends(auth.require_owner),
) -> HTMLResponse:
    auth.check_csrf(request, csrf_token)

    if field == "weight":
        await repo.set_author_flags(tg_user_id, weight=float(value))
    elif field == "muted":
        await repo.set_author_flags(tg_user_id, muted=value == "1")
    elif field == "hide_from_ratings":
        await repo.set_author_flags(tg_user_id, hide_from_ratings=value == "1")
    elif field == "blocked":
        if value == "1":
            await repo.block_user(tg_user_id, reason="из админки")
        else:
            await repo.unblock_user(tg_user_id)
    else:
        raise HTTPException(400, "Неизвестное поле")

    from src.bot.middlewares import settings_cache

    settings_cache.forget()
    found = [a for a in await repo.list_authors() if a["tg_user_id"] == tg_user_id]
    return render(request, "_author_row.html", {"author": found[0]})


# ---------------------------------------------------------------- рейтинги

@app.get("/ratings", response_class=HTMLResponse)
async def ratings_page(
    request: Request, period: str = "week", chat: int | None = None, sort: str = "useful",
    _: dict = Depends(auth.require_owner),
) -> HTMLResponse:
    """Рейтинги с сортировкой по любой метрике (TZ §4.10)."""
    from src.bot.handlers_ratings import resolve_period
    from src.jobs.nominations import BY_KEY, NOMINATIONS, top_of, usefulness_scale

    chats = await repo.list_chats()
    current = next((c for c in chats if c.id == chat), chats[0] if chats else None)
    window = resolve_period(period if period in {"day", "week", "month"} else "week")

    rows = await repo.get_author_stats(
        current.id if current else None,
        date_from=window.date_from,
        date_to=window.date_to,
        hide_optout=False,
    )
    scale = usefulness_scale(rows)
    nomination = BY_KEY.get(sort, BY_KEY["useful"])
    rows.sort(key=nomination.value, reverse=True)

    useful = sorted(
        (r for r in rows if float(r.get("usefulness") or 0) > 0),
        key=lambda r: float(r.get("usefulness") or 0),
        reverse=True,
    )[:10]
    names = [str(r.get("name") or r["tg_user_id"]) for r in useful]
    values = [scale.get(int(r["tg_user_id"]), 0) for r in useful]

    return render(
        request,
        "ratings.html",
        {
            "active": "ratings",
            "chats": chats,
            "chat": current,
            "period": window,
            "period_key": window.key,
            "rows": rows,
            "scale": scale,
            "nominations": NOMINATIONS,
            "sort": nomination.key,
            "tops": {n.key: top_of(n, rows, limit=3) for n in NOMINATIONS},
            "leaders": {
                "spec": {"labels": names, "values": values, "unit": "из 100", "max": 100},
                "rows": list(zip(names, values, strict=True)),
            },
        },
    )


@app.post("/ratings/recalc")
async def ratings_recalc(
    request: Request, chat_id: int = Form(...), days: int = Form(1),
    csrf_token: str = Form(""), _: dict = Depends(auth.require_owner),
) -> Response:
    auth.check_csrf(request, csrf_token)
    from src.jobs.ratings import backfill, recalc_all

    if days > 1:
        await backfill(days)
    else:
        await recalc_all()
    return RedirectResponse("/ratings", status_code=303)


# --------------------------------------------------------------------- Q&A

@app.get("/qa", response_class=HTMLResponse)
async def qa_page(request: Request, _: dict = Depends(auth.require_owner)) -> HTMLResponse:
    return render(request, "qa.html", {"active": "qa", "rows": await repo.list_qa_log()})


# ---------------------------------------------------------------- система

@app.get("/system", response_class=HTMLResponse)
async def system_page(request: Request, _: dict = Depends(auth.require_owner)) -> HTMLResponse:
    now = datetime.now(ZoneInfo(cfg.TZ))
    return render(
        request,
        "system.html",
        {
            "active": "system",
            "processes": labels.describe_states(await repo.get_states(), now),
            "chats": await repo.list_chats(),
            "pending_media": await repo.pending_media_count(),
            "now": now,
        },
    )


@app.post("/system/digest")
async def system_run_digest(
    request: Request, chat_id: int = Form(...), day: str = Form(""),
    csrf_token: str = Form(""), _: dict = Depends(auth.require_owner),
) -> Response:
    """Ручной запуск дайджеста (TZ §4.9). Отправки нет — только пересборка."""
    auth.check_csrf(request, csrf_token)
    from src.digest import pipeline as digest_pipeline

    chat = await repo.get_chat_by_id(chat_id)
    if chat is None:
        raise HTTPException(404, "Группа не найдена")
    try:
        target = date_type.fromisoformat(day) if day else local_today()
    except ValueError:
        raise HTTPException(400, "Дата должна быть вида 2026-09-20") from None
    await digest_pipeline.run_for_chat(chat, target, save=True)
    return RedirectResponse("/digests", status_code=303)


@app.post("/system/reindex")
async def system_reindex(
    request: Request, csrf_token: str = Form(""), _: dict = Depends(auth.require_owner)
) -> Response:
    auth.check_csrf(request, csrf_token)
    from src.rag.index import index_all

    await index_all()
    return RedirectResponse("/system", status_code=303)


@app.get("/system/export/{chat_id}")
async def system_export(
    chat_id: int, day: str | None = None, _: dict = Depends(auth.require_owner)
) -> JSONResponse:
    """Экспорт группы в JSON (TZ §4.9). Отдаётся только владельцу."""
    target = date_type.fromisoformat(day) if day else local_today()
    messages = await repo.get_messages_by_day(chat_id, target)
    return JSONResponse(
        {
            "chat_id": chat_id,
            "day": target.isoformat(),
            "messages": [
                {
                    "tg_msg_id": m.tg_msg_id,
                    "author": m.author_name,
                    "text": m.content,
                    "date": m.date.isoformat(),
                    "thread_id": m.thread_id,
                }
                for m in messages
            ],
        }
    )


# страница участников — отдельным модулем, со своими проверками доступа
from src.web.portal import router as portal_router  # noqa: E402

app.include_router(portal_router)


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=cfg.LOG_LEVEL, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    if not cfg.WEB_SECRET_KEY:
        raise SystemExit("Не задан WEB_SECRET_KEY — см. docs/SETUP.md")
    uvicorn.run(app, host=cfg.WEB_HOST, port=cfg.WEB_PORT)


if __name__ == "__main__":
    main()
