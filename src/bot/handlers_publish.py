"""Кнопка «Опубликовать в группе» под дайджестом в личке владельца.

Два шага — предложение и подтверждение: одно случайное касание не должно
выкладывать текст перед всей группой. Сама публикация и все её правила —
в src/digest/publish.py, здесь только диалог.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Router, types
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.config import cfg
from src.db import repo
from src.db.models import Chat, Digest
from src.digest.publish import PublishResult, can_offer, publish_digest
from src.digest.render import esc_html

log = logging.getLogger(__name__)
router = Router(name="publish")


class PublishCb(CallbackData, prefix="pub"):
    action: str      # ask | yes | no
    digest_id: int


def offer_keyboard(digest_id: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📢 Опубликовать в группе", callback_data=PublishCb(action="ask", digest_id=digest_id)
    )
    return kb.as_markup()


def confirm_keyboard(digest_id: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, публикую", callback_data=PublishCb(action="yes", digest_id=digest_id))
    kb.button(text="✖️ Не надо", callback_data=PublishCb(action="no", digest_id=digest_id))
    kb.adjust(2)
    return kb.as_markup()


def result_text(result: PublishResult) -> str:
    mark = "✅" if result.ok else ("ℹ️" if result.skipped else "⚠️")
    text = f"{mark} {esc_html(result.text)}"
    if result.link:
        text += f'\n<a href="{esc_html(result.link)}">↗️ пост в группе</a>'
    return text


async def offer_publication(bot: Any, chat: Chat, digest: Digest | None) -> bool:
    """Предложить владельцу опубликовать дайджест (режим «по кнопке»)."""
    if chat.publish != "manual" or digest is None or digest.id is None:
        return False
    if not can_offer(chat, digest):
        return False
    title = esc_html(chat.title or str(chat.tg_id))
    await bot.send_message(
        cfg.OWNER_ID,
        f"📢 Опубликовать дайджест за {digest.day:%d.%m} в группе «{title}»?\n"
        "<i>Его увидят все участники. Кнопок оценки там не будет.</i>",
        parse_mode="HTML",
        reply_markup=offer_keyboard(digest.id),
    )
    return True


async def report_auto_publication(bot: Any, result: PublishResult) -> None:
    """Сказать владельцу, что дайджест ушёл в группу сам (или почему не ушёл)."""
    if result.skipped:
        return
    await bot.send_message(
        cfg.OWNER_ID,
        "Автопубликация: " + result_text(result),
        parse_mode="HTML",
        link_preview_options={"is_disabled": True},
    )


@router.callback_query(PublishCb.filter())
async def on_publish(callback: types.CallbackQuery, callback_data: PublishCb, bot: Bot) -> None:
    source = callback.message if isinstance(callback.message, types.Message) else None
    action = callback_data.action
    digest_id = callback_data.digest_id

    if action == "no":
        await callback.answer("Не публикую")
        if source is not None:
            await _edit(source, "Не публикую. Передумаешь — кнопка есть в админке, «Дайджесты».")
        return

    if action == "ask":
        digest = await repo.get_digest_by_id(digest_id)
        chat = await repo.get_chat_by_id(digest.chat_id) if digest else None
        if digest is None or chat is None:
            await callback.answer("Дайджест не найден", show_alert=True)
            return
        if digest.published_at is not None:
            await callback.answer("Уже опубликован", show_alert=True)
            return
        await callback.answer()
        if source is not None:
            title = esc_html(chat.title or str(chat.tg_id))
            await _edit(
                source,
                f"Точно публикую дайджест за {digest.day:%d.%m} в «{title}»? "
                "Увидят все участники.",
                markup=confirm_keyboard(digest_id),
            )
        return

    if action == "yes":
        result = await publish_digest(bot, digest_id)
        await callback.answer("Готово" if result.ok else "Не получилось")
        if source is not None:
            await _edit(source, result_text(result))
        return

    await callback.answer("Непонятное действие", show_alert=True)


async def _edit(message: types.Message, text: str, markup: Any = None) -> None:
    try:
        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        # сообщение могло устареть или уже быть отредактировано
        log.debug("Не удалось обновить сообщение", exc_info=True)
