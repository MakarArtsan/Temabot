"""Кнопки 👍 👎 🔕 под темами дайджеста (TZ §4.7).

Оценки — единственный способ, которым система узнаёт вкус владельца: они идут
в few-shot рубрики и в еженедельный пересчёт весов.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router, types
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.db import repo
from src.digest.render import esc_html

log = logging.getLogger(__name__)
router = Router(name="feedback")

USEFUL = 1
MISS = -1
NEVER = -2

ANSWERS = {
    USEFUL: "Запомнил: такое полезно",
    MISS: "Запомнил: мимо",
    NEVER: "Больше такое показывать не буду",
}


class FeedbackCb(CallbackData, prefix="fb"):
    item_id: int
    value: int


def feedback_keyboard(item_id: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👍", callback_data=FeedbackCb(item_id=item_id, value=USEFUL))
    kb.button(text="👎", callback_data=FeedbackCb(item_id=item_id, value=MISS))
    kb.button(
        text="🔕 не показывать такое", callback_data=FeedbackCb(item_id=item_id, value=NEVER)
    )
    kb.adjust(2, 1)
    return kb.as_markup()


@router.callback_query(FeedbackCb.filter())
async def on_feedback(
    callback: types.CallbackQuery, callback_data: FeedbackCb, bot: Bot
) -> None:
    item = await repo.get_digest_item(callback_data.item_id)
    if item is None:
        await callback.answer("Эта тема уже не найдена", show_alert=True)
        return

    value = callback_data.value
    if value not in ANSWERS:
        await callback.answer("Непонятная оценка", show_alert=True)
        return

    await repo.add_feedback(callback_data.item_id, value)
    total = await repo.count_feedback(item["chat_id"])
    await callback.answer(ANSWERS[value])

    source = callback.message
    if isinstance(source, types.Message):
        mark = {USEFUL: "👍", MISS: "👎", NEVER: "🔕"}[value]
        try:
            await source.edit_text(
                f"{source.html_text}\n\n<i>{mark} оценено, всего оценок: {total}</i>",
                parse_mode="HTML",
            )
        except Exception:
            log.debug("Не удалось обновить сообщение с темой", exc_info=True)


def topic_message(title: str, body: str, item_id: int | None) -> tuple[str, object | None]:
    """Текст темы и клавиатура к ней. Без item_id кнопок нет."""
    text = f"{esc_html(title)}\n{body}" if title else body
    return text, feedback_keyboard(item_id) if item_id else None
