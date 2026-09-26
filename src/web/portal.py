"""Страница участников группы: только чтение (решение владельца, TZ §9).

Пускает тех, кто вошёл через Telegram и сейчас состоит в группе, — и только
в группы, где владелец её открыл (`chats.portal`). Показывает то же, что
увидела бы группа: дайджесты без оценок и служебных признаков, а рейтинги —
только «хорошие» номинации и без скрывшихся по /optout. Настроек, исходных
сообщений, вопросов к боту здесь нет вовсе.

Чего нет или нельзя — всегда 404, а не 403: не подтверждаем постороннему,
что такая группа вообще есть.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.config import cfg
from src.db import repo
from src.db.models import Chat, Digest
from src.digest.render import DigestData
from src.web import auth, membership

log = logging.getLogger(__name__)
router = APIRouter()

ARCHIVE_DAYS = 60
ACTIVITY_DAYS = 30
LEADERS = 10


@dataclass(slots=True)
class Viewer:
    uid: int
    is_owner: bool


def viewer_of(request: Request) -> Viewer:
    session = auth.current_viewer(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Нужен вход через Telegram")
    uid = auth.session_uid(session)
    return Viewer(uid=uid, is_owner=uid == cfg.OWNER_ID)


async def open_chat(request: Request, chat_id: int) -> tuple[Viewer, Chat]:
    """Группа, которую этому человеку можно смотреть, — иначе 404."""
    viewer = viewer_of(request)
    chat = await repo.get_chat_by_id(chat_id)
    if chat is None:
        raise HTTPException(status_code=404)
    if not viewer.is_owner:
        # режим читаем из базы на каждом запросе: закрыл владелец — закрыто сразу
        if chat.portal not in ("digests", "all"):
            raise HTTPException(status_code=404)
        if not await membership.is_member(chat, viewer.uid):
            raise HTTPException(status_code=404)
    return viewer, chat


def ratings_visible(chat: Chat, viewer: Viewer) -> bool:
    return viewer.is_owner or chat.portal == "all"


def digest_data(digest: Digest | None, chat: Chat) -> DigestData | None:
    """Структура дайджеста; у совсем старых записей её может не быть."""
    if digest is None or not digest.payload:
        return None
    try:
        return DigestData.from_dict(digest.payload).titled(chat.title)
    except (TypeError, ValueError, KeyError):
        log.warning("Дайджест %s без полной структуры", digest.id)
        return None


def archive_row(digest: Digest) -> dict[str, Any]:
    payload = digest.payload or {}
    highlights = list(payload.get("highlights") or [])
    topics = list(payload.get("topics") or [])
    first_topic = str((topics[0] or {}).get("title") or "") if topics else ""
    return {
        "day": digest.day,
        "msg_count": int(digest.msg_count or payload.get("msg_count") or 0),
        "topics_count": len(topics),
        "headline": str(highlights[0]) if highlights else (first_topic or "Тихий день"),
    }


MESSAGES_UNIT = ["сообщение", "сообщения", "сообщений"]


def daily_series(
    by_day: dict[date_type, Any],
    last_day: date_type,
    days: int,
    *,
    unit: Any = MESSAGES_UNIT,
    fmt: Any = str,
) -> dict[str, Any]:
    """Ряд по дням без пропусков: день без данных — это ноль, а не дыра на оси.

    `spec` уходит в график, `rows` — в таблицу под ним (для тех, кому график не виден).
    """
    series = [last_day - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    values = [by_day.get(day, 0) or 0 for day in series]
    return {
        "spec": {"labels": [day.isoformat() for day in series], "values": values, "unit": unit},
        "rows": [
            (day.strftime("%d.%m"), fmt(value)) for day, value in zip(series, values, strict=True)
        ],
        "has_data": any(values),
    }


def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    from src.web.app import TEMPLATES

    return TEMPLATES.TemplateResponse(request, name, context, status_code=status)


@router.get("/g", response_class=HTMLResponse)
async def portal_home(request: Request) -> Response:
    viewer = viewer_of(request)
    chats = await membership.visible_chats(viewer.uid)
    if len(chats) == 1:
        return RedirectResponse(f"/g/{chats[0].id}", status_code=303)
    return render(request, "portal/index.html", {"chats": chats, "is_owner": viewer.is_owner})


@router.get("/g/{chat_id}", response_class=HTMLResponse)
async def portal_group(request: Request, chat_id: int) -> HTMLResponse:
    from src.web.app import local_today

    viewer, chat = await open_chat(request, chat_id)
    digests = await repo.list_chat_digests(chat.id, limit=ARCHIVE_DAYS)
    per_day = await repo.messages_per_day(chat.id, days=ACTIVITY_DAYS)
    counts = {row["day"]: int(row["count"]) for row in per_day}

    return render(
        request,
        "portal/group.html",
        {
            "chat": chat,
            "is_owner": viewer.is_owner,
            "ratings_visible": ratings_visible(chat, viewer),
            "digests": [archive_row(d) for d in digests],
            "latest": digest_data(digests[0], chat) if digests else None,
            "activity": daily_series(counts, local_today(), ACTIVITY_DAYS),
        },
    )


@router.get("/g/{chat_id}/d/{day}", response_class=HTMLResponse)
async def portal_digest(request: Request, chat_id: int, day: str) -> HTMLResponse:
    viewer, chat = await open_chat(request, chat_id)
    try:
        target = date_type.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=404) from None

    data = digest_data(await repo.get_digest(chat.id, target), chat)
    if data is None:
        raise HTTPException(status_code=404)

    days = [d.day for d in await repo.list_chat_digests(chat.id, limit=ARCHIVE_DAYS)]
    return render(
        request,
        "portal/digest.html",
        {
            "chat": chat,
            "is_owner": viewer.is_owner,
            "ratings_visible": ratings_visible(chat, viewer),
            "day": target,
            "data": data,
            "prev_day": max((d for d in days if d < target), default=None),
            "next_day": min((d for d in days if d > target), default=None),
        },
    )


@router.get("/g/{chat_id}/ratings", response_class=HTMLResponse)
async def portal_ratings(request: Request, chat_id: int, period: str = "week") -> HTMLResponse:
    from src.bot.handlers_ratings import PUBLIC_NOMINATIONS, resolve_period
    from src.jobs.nominations import NOMINATIONS, top_of, usefulness_scale

    viewer, chat = await open_chat(request, chat_id)
    if not ratings_visible(chat, viewer):
        raise HTTPException(status_code=404)

    window = resolve_period(period if period in {"day", "week", "month"} else "week")
    # скрывшиеся по /optout сюда не попадают — это делает сам запрос
    rows = await repo.get_author_stats(
        chat.id, date_from=window.date_from, date_to=window.date_to, hide_optout=True
    )
    scale = usefulness_scale(rows)
    tops = [
        (n, top_of(n, rows, limit=3)) for n in NOMINATIONS if n.key in PUBLIC_NOMINATIONS
    ]

    useful = sorted(
        (r for r in rows if float(r.get("usefulness") or 0) > 0),
        key=lambda r: float(r.get("usefulness") or 0),
        reverse=True,
    )[:LEADERS]
    names = [str(r.get("name") or "Участник") for r in useful]
    values = [scale.get(int(r["tg_user_id"]), 0) for r in useful]

    return render(
        request,
        "portal/ratings.html",
        {
            "chat": chat,
            "is_owner": viewer.is_owner,
            "ratings_visible": True,
            "period": window,
            "has_rows": bool(rows),
            "scale": scale,
            "tops": [(n, top) for n, top in tops if top],
            "leaders": {
                "spec": {"labels": names, "values": values, "unit": "из 100", "max": 100},
                "rows": list(zip(names, values, strict=True)),
            },
        },
    )
