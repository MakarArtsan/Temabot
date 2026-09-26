"""Авторизация: Telegram Login Widget (TZ §4.9, §9).

В админке лежит содержимое закрытых групп, поэтому незащищённых страниц нет
вовсе: единственный публичный маршрут — сама страница входа.

Виджет отдаёт данные пользователя, подписанные ключом из токена бота. Проверяем
подпись и свежесть. Админка — только для OWNER_ID: это проверяется при каждом
запросе (`current_user`), а не по отметке в куке.

Участник группы тоже может войти — на страницу участников, только чтение
(решение владельца, TZ §9). Его кука годится лишь для `current_viewer`, а
состоит ли он в группе, страница участников проверяет сама при каждом запросе.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from src.config import cfg

log = logging.getLogger(__name__)

COOKIE_NAME = "tgd_session"
SESSION_MAX_AGE = 14 * 24 * 3600     # две недели
AUTH_MAX_AGE = 24 * 3600             # данные виджета старше суток не принимаем
CSRF_FIELD = "csrf_token"


class AuthError(Exception):
    """Вход не удался. Текст показывается на странице входа."""


def _serializer() -> URLSafeTimedSerializer:
    if not cfg.WEB_SECRET_KEY:
        raise RuntimeError(
            "Не задан WEB_SECRET_KEY: без него куку можно подделать. См. docs/SETUP.md"
        )
    return URLSafeTimedSerializer(cfg.WEB_SECRET_KEY, salt="tgd-admin")


def check_telegram_auth(data: dict[str, Any], *, bot_token: str | None = None,
                        now: float | None = None) -> int:
    """Проверить подпись виджета и вернуть tg_user_id.

    Алгоритм Telegram: ключ — SHA256 от токена бота, сообщение — строки
    `ключ=значение`, отсортированные по ключу, кроме самого `hash`.
    """
    token = bot_token if bot_token is not None else cfg.BOT_TOKEN
    if not token:
        raise AuthError("Не задан BOT_TOKEN — проверить подпись нечем")

    received = str(data.get("hash") or "")
    if not received:
        raise AuthError("Телеграм не передал подпись")

    payload = {k: v for k, v in data.items() if k != "hash" and v is not None}
    check_string = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received):
        raise AuthError("Подпись не сходится")

    try:
        auth_date = float(payload.get("auth_date", 0))
    except (TypeError, ValueError):
        raise AuthError("Непонятная дата входа") from None

    current = now if now is not None else time.time()
    if current - auth_date > AUTH_MAX_AGE:
        raise AuthError("Данные входа устарели, попробуй ещё раз")

    try:
        user_id = int(payload.get("id", 0))
    except (TypeError, ValueError):
        raise AuthError("Непонятный идентификатор") from None

    if not user_id:
        raise AuthError("Телеграм не передал идентификатор")
    return user_id


def issue_session(user_id: int) -> str:
    """Подписанная кука с идентификатором и свежим CSRF-токеном."""
    return _serializer().dumps({"uid": user_id, "csrf": secrets.token_urlsafe(24)})


def session_uid(session: dict[str, Any] | None) -> int:
    try:
        return int((session or {}).get("uid", 0))
    except (TypeError, ValueError):
        return 0


def read_session(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except SignatureExpired:
        log.info("Сессия админки истекла")
        return None
    except BadSignature:
        log.warning("Подделанная кука админки")
        return None
    return data if isinstance(data, dict) else None


def current_viewer(request: Request) -> dict[str, Any] | None:
    """Любой вошедший через Telegram: владелец или участник группы.

    Годится только для страницы участников — и там же проверяется членство.
    """
    session = read_session(request.cookies.get(COOKIE_NAME))
    if session is None or not session_uid(session):
        return None
    return session


def current_user(request: Request) -> dict[str, Any] | None:
    """Владелец — единственный, кому открыта админка."""
    session = current_viewer(request)
    if session is None:
        return None
    if session_uid(session) != cfg.OWNER_ID:
        # чужая кука или владелец сменился в конфиге — в админку не пускаем
        return None
    return session


def require_owner(request: Request) -> dict[str, Any]:
    """Зависимость FastAPI: без владельца страница не отдаётся."""
    session = current_user(request)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Нужен вход через Telegram",
            headers={"Location": "/login"},
        )
    return session


def check_csrf(request: Request, token: str | None) -> None:
    """Любая мутация должна нести токен из сессии.

    Сравниваем байты: `compare_digest` на строках падает с TypeError, если в
    присланном значении есть не-ASCII символы, — а прислать туда можно что угодно.
    """
    session = current_user(request)
    expected = (session or {}).get("csrf")
    if not expected or not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Неверный CSRF-токен")

    if not hmac.compare_digest(str(expected).encode("utf-8"), str(token).encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Неверный CSRF-токен")
