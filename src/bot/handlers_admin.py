"""Управление группами и доступом (TZ §4.8).

Бота могут добавить в любую группу. По умолчанию он там молчит, пока владелец
не разрешит — иначе бот начал бы работать в чате, о котором владелец не знает.
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router, types
from aiogram.filters import (
    ADMINISTRATOR,
    IS_NOT_MEMBER,
    MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
)
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.bot.middlewares import settings_cache
from src.config import cfg
from src.db import repo
from src.digest.render import esc_html

log = logging.getLogger(__name__)
router = Router(name="admin")

COPIER_LABELS = {"allow": "✅ разрешён", "deny": "🚫 запрещён", "ask": "⏳ ждёт решения"}


class GroupCb(CallbackData, prefix="grp"):
    """Кнопки под запросом о новой группе и в /groups."""

    action: str      # allow | deny | collect | digest
    chat_id: int


def _decision_keyboard(chat_tg_id: int) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Разрешить", callback_data=GroupCb(action="allow", chat_id=chat_tg_id))
    kb.button(
        text="🚫 Запретить и выйти", callback_data=GroupCb(action="deny", chat_id=chat_tg_id)
    )
    kb.adjust(1)
    return kb.as_markup()


@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> (MEMBER | ADMINISTRATOR)))
async def on_added_to_chat(event: types.ChatMemberUpdated, bot: Bot) -> None:
    """Бота куда-то добавили (TZ §4.8)."""
    chat = event.chat
    if chat.type == "private":
        return

    stored = await repo.get_or_create_chat(chat.id, chat.title)
    settings_cache.forget(chat.id)
    who = event.from_user
    who_name = f"@{who.username}" if who and who.username else (who.full_name if who else "?")

    if stored.copier == "deny":
        # решение уже принято раньше — выходим молча, не дожидаясь вопросов
        log.info("Группа %s в запрете, выхожу", chat.id)
        await bot.leave_chat(chat.id)
        await _notify_owner(
            bot,
            f"Меня добавили в «{esc_html(chat.title or str(chat.id))}», "
            f"но группа в запрете — вышел.",
        )
        return

    if stored.copier == "allow":
        await _notify_owner(
            bot,
            f"Меня добавили в «{esc_html(chat.title or str(chat.id))}» "
            f"(добавил {esc_html(who_name)}). Группа разрешена, работаю.",
        )
        return

    # ask: молчим до решения владельца
    await _notify_owner(
        bot,
        f"Бота добавили в «{esc_html(chat.title or str(chat.id))}»\n"
        f"Кто добавил: {esc_html(who_name)}\n"
        f"ID группы: <code>{chat.id}</code>\n\n"
        f"До решения я там молчу.",
        markup=_decision_keyboard(chat.id),
    )


@router.my_chat_member(ChatMemberUpdatedFilter((MEMBER | ADMINISTRATOR) >> IS_NOT_MEMBER))
async def on_removed_from_chat(event: types.ChatMemberUpdated, bot: Bot) -> None:
    await _notify_owner(
        bot, f"Меня убрали из «{esc_html(event.chat.title or str(event.chat.id))}»."
    )


async def _notify_owner(bot: Bot, text: str, markup: Any = None) -> None:
    """Все уведомления идут только владельцу в личку (TZ §9)."""
    if not cfg.OWNER_ID:
        log.warning("OWNER_ID не задан, уведомление некому отправить")
        return
    try:
        await bot.send_message(cfg.OWNER_ID, text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        log.exception("Не удалось уведомить владельца")


@router.callback_query(GroupCb.filter())
async def on_group_decision(
    callback: types.CallbackQuery, callback_data: GroupCb, bot: Bot
) -> None:
    """Кнопки «Разрешить» / «Запретить и выйти» и переключатели в /groups."""
    chat_tg_id = callback_data.chat_id
    action = callback_data.action
    chat = await repo.get_chat_by_tg_id(chat_tg_id)
    if chat is None:
        await callback.answer("Группа не найдена", show_alert=True)
        return

    if action == "allow":
        await repo.set_chat_flags(chat_tg_id, copier="allow")
        answer = "Разрешил"
    elif action == "deny":
        await repo.set_chat_flags(chat_tg_id, copier="deny")
        try:
            await bot.leave_chat(chat_tg_id)
            answer = "Запретил и вышел"
        except Exception:
            log.exception("Не удалось выйти из группы %s", chat_tg_id)
            answer = "Запретил, но выйти не получилось"
    elif action == "collect":
        await repo.set_chat_flags(chat_tg_id, collect=not chat.collect)
        answer = "Сбор включён" if not chat.collect else "Сбор выключен"
    elif action == "digest":
        await repo.set_chat_flags(chat_tg_id, digest=not chat.digest)
        answer = "Дайджест включён" if not chat.digest else "Дайджест выключен"
    else:
        await callback.answer("Непонятное действие", show_alert=True)
        return

    settings_cache.forget(chat_tg_id)
    await callback.answer(answer)

    source = callback.message
    if isinstance(source, types.Message):
        try:
            await source.edit_text(
                f"{source.html_text}\n\n<i>{esc_html(answer)}</i>", parse_mode="HTML"
            )
        except Exception:
            # сообщение могло быть уже отредактировано, удалено или слишком старым
            log.debug("Не удалось обновить сообщение", exc_info=True)


@router.message(Command("groups"))
async def on_groups(message: types.Message) -> None:
    """Список групп с переключателями (TZ §4.8)."""
    chats = await repo.list_chats()
    if not chats:
        await message.answer("Пока не знаю ни одной группы.")
        return

    for chat in chats:
        kb = InlineKeyboardBuilder()
        kb.button(
            text=("🟢 сбор вкл" if chat.collect else "⚪️ сбор выкл"),
            callback_data=GroupCb(action="collect", chat_id=chat.tg_id),
        )
        kb.button(
            text=("🟢 дайджест вкл" if chat.digest else "⚪️ дайджест выкл"),
            callback_data=GroupCb(action="digest", chat_id=chat.tg_id),
        )
        if chat.copier != "allow":
            kb.button(
                text="✅ разрешить копировщик",
                callback_data=GroupCb(action="allow", chat_id=chat.tg_id),
            )
        else:
            kb.button(
                text="🚫 запретить и выйти",
                callback_data=GroupCb(action="deny", chat_id=chat.tg_id),
            )
        kb.adjust(2, 1)

        await message.answer(
            f"<b>{esc_html(chat.title or str(chat.tg_id))}</b>\n"
            f"<code>{chat.tg_id}</code>\n"
            f"копировщик: {COPIER_LABELS.get(chat.copier, chat.copier)}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


@router.message(Command("ban"))
async def on_ban(message: types.Message, command: CommandObject) -> None:
    """Запретить пользователю копировщик (TZ §4.8).

    Bot API не умеет искать пользователя по @username, поэтому надёжнее всего
    ответить командой на его сообщение. Поиск по имени работает только для тех,
    кого уже видел коллектор.
    """
    target_id, label = await _resolve_user(message, command)
    if target_id is None:
        await message.answer(
            "Кого банить? Ответь этой командой на сообщение, "
            "или напиши <code>/ban 12345678</code>, или <code>/ban Вася</code>.",
            parse_mode="HTML",
        )
        return

    await repo.block_user(target_id, reason=(command.args or "").strip() or None)
    settings_cache.forget()
    await message.answer(f"Запретил копировщик для {esc_html(label)}.", parse_mode="HTML")


@router.message(Command("unban"))
async def on_unban(message: types.Message, command: CommandObject) -> None:
    target_id, label = await _resolve_user(message, command)
    if target_id is None:
        await message.answer("Кого разбанить? Укажи id, имя или ответь на сообщение.")
        return

    removed = await repo.unblock_user(target_id)
    settings_cache.forget()
    await message.answer(
        f"Снял запрет с {esc_html(label)}." if removed else "Он и не был в списке."
    )


@router.message(Command("banned"))
async def on_banned(message: types.Message) -> None:
    blocked = await repo.list_blocked()
    if not blocked:
        await message.answer("Блок-лист пуст.")
        return
    lines = ["<b>Кому запрещён копировщик</b>", ""]
    for row in blocked:
        name = row["name"] or str(row["tg_user_id"])
        reason = f" — {row['reason']}" if row["reason"] else ""
        lines.append(f"• {esc_html(name)} (<code>{row['tg_user_id']}</code>){esc_html(reason)}")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _resolve_user(
    message: types.Message, command: CommandObject
) -> tuple[int | None, str]:
    """Найти, о ком речь: реплай, числовой id или имя из базы."""
    target = message.reply_to_message
    if target is not None and target.from_user is not None:
        return target.from_user.id, target.from_user.full_name

    query = (command.args or "").strip()
    if not query:
        return None, ""

    first = query.split()[0]
    if first.lstrip("-").isdigit():
        return int(first), first

    author = await repo.find_author(first)
    if author is None:
        return None, ""
    return author.tg_user_id, author.name or str(author.tg_user_id)


@router.message(F.chat.type.in_({"group", "supergroup"}), Command("id"))
async def on_chat_id(message: types.Message) -> None:
    """Узнать id группы, не роясь в логах."""
    if message.from_user and message.from_user.id == cfg.OWNER_ID:
        await message.reply(f"<code>{message.chat.id}</code>", parse_mode="HTML")
