"""
src/bot/handlers_copier.py — бот-копировщик, переписанный под общий проект.

Поведение для группы не изменилось: упомяни бота — получишь кнопку со ссылкой
на Telegraph, откуда текст копируется. Добавлено:
  * `@bot` реплаем на чужое сообщение → копируется то сообщение;
  * скопированное из отслеживаемой группы помечается в БД как важное;
  * кнопка «Что обсуждали вокруг» (только для владельца, ответ в личку).

Подключение в src/bot/main.py:
    from src.bot import handlers_copier as copier
    dp.include_router(copier.router)      # ПЕРВЫМ, до роутеров Q&A
    dp.include_router(qa.router)
    await copier.init_copier(bot)         # до dp.start_polling
"""
from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import Bot, Router, types
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telegraph import Telegraph

from src.config import cfg
from src.db import repo
from src.rag.answer import answer_about_thread

log = logging.getLogger(__name__)
router = Router(name="copier")

_telegraph: Telegraph | None = None
_bot_username: str = ""


class AroundCb(CallbackData, prefix="around"):
    chat_id: int
    msg_id: int


async def init_copier(bot: Bot) -> None:
    """Один раз при старте: кэшируем username и поднимаем Telegraph."""
    global _telegraph, _bot_username
    _bot_username = (await bot.get_me()).username or ""

    tg = Telegraph(access_token=cfg.TELEGRAPH_TOKEN or None)
    if not cfg.TELEGRAPH_TOKEN:
        acc = await asyncio.to_thread(
            tg.create_account, short_name=cfg.TELEGRAPH_SHORT_NAME
        )
        log.warning(
            "Создан аккаунт Telegraph. Добавь в .env: TELEGRAPH_TOKEN=%s",
            acc["access_token"],
        )
    _telegraph = tg


class MentionsMe(Filter):
    """True, если в тексте или подписи есть @упоминание этого бота.

    entity.extract_from корректно считает offset в UTF-16 —
    ручной срез строки ломался, если до упоминания стоял эмодзи.
    """

    async def __call__(self, message: types.Message) -> bool:
        text = message.text or message.caption
        entities = message.entities or message.caption_entities or []
        if not text or not _bot_username:
            return False
        me = f"@{_bot_username}".lower()
        return any(
            e.type == "mention" and e.extract_from(text).lower() == me
            for e in entities
        )


def _strip_mention(text: str) -> str:
    return re.sub(rf"@{re.escape(_bot_username)}\b", "", text, flags=re.I).strip()


async def _make_page(text: str) -> str:
    assert _telegraph is not None, "init_copier() не вызван"
    content = f"<code>{html.escape(text).replace(chr(10), '<br>')}</code>"
    page = await asyncio.to_thread(
        _telegraph.create_page,
        title="Текст для копирования",
        html_content=content,
    )
    return page["url"]


@router.message(MentionsMe())
async def on_mention(message: types.Message) -> None:
    own_text = _strip_mention(message.text or message.caption or "")
    target = message.reply_to_message

    if own_text:
        text, source_msg = own_text, message
    elif target and (parent_text := target.text or target.caption):
        # моржовое присваивание, чтобы mypy видел: здесь уже не None
        text, source_msg = parent_text, target
    else:
        await message.reply(
            "Напиши текст после упоминания или ответь упоминанием на сообщение."
        )
        return

    try:
        url = await _make_page(text)
    except Exception:
        log.exception("Telegraph: не удалось создать страницу")
        await message.reply("Упс, что-то пошло не так. Попробуйте ещё раз.")
        return

    tracked = source_msg.chat.id == cfg.TG_GROUP_ID
    if tracked:
        try:
            await repo.mark_copied(
                chat_tg_id=source_msg.chat.id,
                tg_msg_id=source_msg.message_id,
                text=text,
                tg_user_id=source_msg.from_user.id if source_msg.from_user else None,
                author_name=source_msg.from_user.full_name if source_msg.from_user else None,
                date=source_msg.date,
            )
        except Exception:
            # БД не должна ломать копирование
            log.exception("mark_copied упал")

    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Копировать текст", url=url)
    if tracked:
        kb.button(
            text="🧵 Что обсуждали вокруг",
            callback_data=AroundCb(
                chat_id=source_msg.chat.id, msg_id=source_msg.message_id
            ),
        )
    kb.adjust(1)
    await message.reply("Готово!", reply_markup=kb.as_markup())


@router.callback_query(AroundCb.filter())
async def on_around(
    cb: types.CallbackQuery, callback_data: AroundCb, bot: Bot
) -> None:
    if cb.from_user.id != cfg.OWNER_ID:
        await cb.answer("Доступно только владельцу", show_alert=True)
        return

    await cb.answer("Собираю контекст, пришлю в личку")
    try:
        summary_html = await answer_about_thread(
            callback_data.chat_id, callback_data.msg_id
        )
        await bot.send_message(
            cfg.OWNER_ID,
            summary_html,
            parse_mode="HTML",
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
        )
    except TelegramForbiddenError:
        log.warning("Владелец не нажал /start в личке бота — ответ не доставлен")
    except Exception:
        log.exception("answer_about_thread упал")
