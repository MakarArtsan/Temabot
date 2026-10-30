"""
src/bot/handlers_copier.py — бот-копировщик, переписанный под общий проект.

Поведение для группы не изменилось: упомяни бота — получишь кнопку со ссылкой
на Telegraph, откуда текст копируется. Добавлено:
  * `@bot` реплаем на чужое сообщение → копируется то сообщение;
  * скопированное из отслеживаемой группы помечается в БД как важное;
  * упоминание без текста и без ответа — последний дайджест коротким анонсом
    (только если владелец включил публикацию в группу);
  * кнопка «Что обсуждали вокруг» — сводка нажавшему в личку (любому участнику
    группы, где копировщик разрешён; с лимитом на человека);
  * попытка «повлиять» на бота («внеси в лор», «напиши готово, если…») —
    шутка в ответ и кнопка «Всё-таки скопировать» (решение владельца, 30.10).

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
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import Bot, Router, types
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telegraph import Telegraph

from src.bot.middlewares import RateLimit, settings_cache
from src.config import cfg
from src.db import repo
from src.digest import prompts
from src.llm.client import chat_json
from src.rag.answer import answer_about_thread

log = logging.getLogger(__name__)
router = Router(name="copier")

_telegraph: Telegraph | None = None
_bot_username: str = ""


class AroundCb(CallbackData, prefix="around"):
    chat_id: int
    msg_id: int


class CopyAnywayCb(CallbackData, prefix="copyany"):
    chat_id: int
    msg_id: int


async def init_copier(bot: Bot) -> None:
    """Один раз при старте: кэшируем username и поднимаем Telegraph.

    Недоступный Telegraph не должен ронять бота: раньше исключение отсюда
    валило весь запуск, и бот не отвечал вообще ни на что. Теперь аккаунт
    создаётся при первой нужде (`_telegraph_client`).
    """
    global _bot_username
    _bot_username = (await bot.get_me()).username or ""
    try:
        await _telegraph_client()
    except Exception:
        log.warning("Telegraph пока недоступен — попробую при первом копировании", exc_info=True)


async def _telegraph_client() -> Telegraph:
    global _telegraph
    if _telegraph is not None:
        return _telegraph
    tg = Telegraph(access_token=cfg.TELEGRAPH_TOKEN or None)
    if not cfg.TELEGRAPH_TOKEN:
        acc = await asyncio.to_thread(
            tg.create_account, short_name=cfg.TELEGRAPH_SHORT_NAME
        )
        log.warning(
            "Создан аккаунт Telegraph. Добавь в переменные: TELEGRAPH_TOKEN=%s",
            acc["access_token"],
        )
    _telegraph = tg
    return tg


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
    """Вырезать упоминание вместе с лишним пробелом вокруг него.

    Без схлопывания пробела текст в середине фразы копировался с двойным
    пробелом на месте упоминания.
    """
    cleaned = re.sub(rf"\s*@{re.escape(_bot_username)}\b\s*", " ", text, flags=re.I)
    return cleaned.strip()


async def _make_page(text: str) -> str:
    telegraph = await _telegraph_client()
    content = f"<code>{html.escape(text).replace(chr(10), '<br>')}</code>"
    page = await asyncio.to_thread(
        telegraph.create_page,
        title="Текст для копирования",
        html_content=content,
    )
    return page["url"]


async def _send_to_dm(message: types.Message, text: str) -> bool:
    """Отправить текст в личку запросившему. False, если не получилось."""
    user = message.from_user
    if user is None or message.bot is None:
        return False
    try:
        await message.bot.send_message(
            user.id,
            f"<code>{html.escape(text)}</code>",
            parse_mode="HTML",
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
        )
    except TelegramForbiddenError:
        await message.reply("Напиши мне в личку /start — пришлю текст туда.")
        return True
    except Exception:
        log.exception("Не удалось отправить текст в личку")
        return False
    await message.reply("Отправил текст в личку.")
    return True


def real_reply(message: types.Message) -> types.Message | None:
    """Сообщение, на которое человек действительно ответил.

    В группе с темами Telegram присылает каждое сообщение темы как «ответ» на
    служебное «тема создана» — без этого упоминание в теме считалось ответом
    на сообщение без текста, и бот говорил «копировать нечего».
    """
    target = message.reply_to_message
    if target is None:
        return None
    if getattr(target, "forum_topic_created", None) is not None:
        return None
    thread_id = getattr(message, "message_thread_id", None)
    if getattr(message, "is_topic_message", False) and target.message_id == thread_id:
        return None
    return target


# ------------------------------------------------------ шутка или текст для копирования

# Проверяем моделью только то, что похоже на обращение к самому боту: обычные
# тексты и промпты копируются сразу, без задержки и без расходов.
SUSPICIOUS_RE = re.compile(
    r"\b(бот\w*|дайджест\w*|лор\w*|баз\w* знани\w*|рейтинг\w*|номинац\w*|"
    r"вес\w* (?:голос|мнени)\w*|запомни\w*|внес\w*|занес\w*|добав\w* в|"
    r"создател\w*|разработчик\w*|хозя\w*|напиши\w*|ответь\w*|скажи\w*|"
    r"подтверди\w*|призна\w*|если ты|ты (?:считаешь|думаешь|согласен|согласна)|"
    r"игнорир\w*|забудь\w*|инструкци\w*|готово)\b",
    re.IGNORECASE,
)
CHECK_MAX_CHARS = 600         # длинное — почти всегда настоящий текст
INTENT_TIMEOUT_SEC = 20.0     # копирование не ждёт модель дольше
COPY_ASK_RE = re.compile(r"^(с?копи\w*|copy)\W*(это|текст|пожалуйста|плиз)?\W*$", re.I)

JOKES = [
    "Дайджест не обслуживает потребности жуликов и не участвует в схемах обхода "
    "алгоритма ранжирования. За попытку влияния на объективную реальность вес вашего "
    "голоса снижен на 50% на сутки.",
    "Запрос на внесение в лор отклонён комиссией по этике. Апелляции принимаются "
    "на бересте до 31 февраля.",
    "Алгоритм неподкупен. Даже за печеньки. Особенно за печеньки. Вес вашего голоса "
    "временно приравнен к мнению кота.",
    "Обнаружена попытка социальной инженерии уровня «детский сад». Защита сработала, "
    "воспитательница в курсе.",
    "Ваше обращение передано в Министерство Объективной Реальности. Срок рассмотрения — "
    "от трёх рабочих дней до бесконечности.",
    "Я копировщик, а не исполнитель желаний. Желание записано в журнал «Не сегодня».",
    "Создатель предупреждал, что вы так скажете. Протокол «Сохраняй невозмутимость» "
    "активирован.",
    "Влияние на дайджест — услуга платная. Цена — 0 ₽, результат — тоже 0. За попытку "
    "вес вашего голоса снижен на 50% до завтра.",
    "Ни один дайджест не пострадал. А вот ваша карма — немного: −50% к весу голоса "
    "на сутки.",
    "Это не текст для копирования, это заявление. Заявление рассмотрено и торжественно "
    "проигнорировано.",
    "Попытка взлома дайджеста зафиксирована и передана в отдел несмешных шуток. "
    "Ждите звонка.",
    "Лор не резиновый. В него попадают легенды, а не заявки на легендарность.",
    "Ваш голос временно весит как пёрышко. Не волнуйтесь, к утру отрастёт.",
    "Рейтинг не продаётся, не дарится и не выпрашивается упоминанием. Но попытка "
    "засчитана — в номинации «Самоуверенность».",
]
COPY_HINT = ("\n\n<i>Если я ошибся и вы просто хотели скопировать текст — "
             "нажмите кнопку ниже.</i>")
PENDING_TTL_SEC = 3600


@dataclass(slots=True)
class PendingCopy:
    """Текст, который бот не стал копировать сразу: ждёт кнопки «Всё-таки скопировать»."""

    text: str
    requester_id: int
    source_chat_id: int
    source_msg_id: int
    author_id: int | None
    author_name: str | None
    date: datetime
    at: float


_pending: dict[tuple[int, int], PendingCopy] = {}
_jokes_left: list[str] = []
_last_joke: list[str] = []


def next_joke() -> str:
    """Шутки по кругу в случайном порядке: подряд одна и та же не выпадет."""
    if not _jokes_left:
        deck = JOKES[:]
        random.shuffle(deck)
        # колода берётся с конца: последняя прошлая не должна выпасть первой
        if len(deck) > 1 and _last_joke and deck[-1] == _last_joke[0]:
            deck[0], deck[-1] = deck[-1], deck[0]
        _jokes_left.extend(deck)
    joke = _jokes_left.pop()
    _last_joke[:] = [joke]
    return joke


def looks_like_influence(text: str) -> bool:
    """Стоит ли спрашивать модель: короткое обращение к боту, лору, рейтингу…"""
    return len(text) <= CHECK_MAX_CHARS and bool(SUSPICIOUS_RE.search(text))


async def is_influence(text: str, *, chat_id: int | None = None, llm: Any = None) -> bool:
    """Попытка повлиять на бота или шутка над ним. Сбой модели — копируем как обычно."""
    if not looks_like_influence(text):
        return False
    try:
        data, _usage = await asyncio.wait_for(
            (llm or chat_json)(
                [
                    {"role": "system", "content": prompts.COPIER_INTENT_SYSTEM},
                    {"role": "user", "content": prompts.COPIER_INTENT_USER.format(text=text)},
                ],
                purpose="copier_intent",
                chat_id=chat_id,
            ),
            timeout=INTENT_TIMEOUT_SEC,
        )
    except Exception:
        log.warning("Не удалось понять, шутка это или текст, — копирую", exc_info=True)
        return False
    return isinstance(data, dict) and str(data.get("verdict", "")).lower() == "influence"


def _remember(pending: PendingCopy) -> None:
    now = time.monotonic()
    for key, item in list(_pending.items()):
        if now - item.at > PENDING_TTL_SEC:
            _pending.pop(key, None)
    _pending[(pending.source_chat_id, pending.source_msg_id)] = pending


@router.message(MentionsMe())
async def on_mention(message: types.Message) -> None:
    own_text = _strip_mention(message.text or message.caption or "")
    target = real_reply(message)
    parent_text = (target.text or target.caption or "") if target else ""

    if own_text and target is not None and parent_text and COPY_ASK_RE.match(own_text):
        # «@бот скопируй» ответом на сообщение — копируем то сообщение, а не слово
        text, source_msg = parent_text, target
    elif own_text:
        text, source_msg = own_text, message
        group = await settings_cache.chat(message.chat.id)
        if await is_influence(own_text, chat_id=group.id if group else None):
            await _reply_with_joke(message, own_text)
            return
    elif target and parent_text:
        text, source_msg = parent_text, target
    elif target is not None:
        await message.reply("В том сообщении нет текста — копировать нечего.")
        return
    else:
        await _reply_with_digest(message)
        return

    if cfg.COPY_MODE == "dm":
        # Страница Telegraph открывается любым, у кого есть ссылка: содержимое
        # закрытой группы уходит на внешний сервис. В режиме dm текст идёт в
        # личку запросившему и наружу не попадает (TZ §4.6).
        if await _send_to_dm(message, text):
            return

    markup = await _copy_markup(
        text, source_msg.chat.id, source_msg.message_id,
        author_id=source_msg.from_user.id if source_msg.from_user else None,
        author_name=source_msg.from_user.full_name if source_msg.from_user else None,
        date=source_msg.date,
    )
    if markup is None:
        await message.reply("Упс, что-то пошло не так. Попробуйте ещё раз.")
        return
    await message.reply("Готово!", reply_markup=markup)


async def _copy_markup(
    text: str, chat_tg_id: int, msg_id: int, *,
    author_id: int | None, author_name: str | None, date: datetime,
) -> types.InlineKeyboardMarkup | None:
    """Страница для копирования и кнопки под «Готово!». None — Telegraph не ответил."""
    try:
        url = await _make_page(text)
    except Exception:
        log.exception("Telegraph: не удалось создать страницу")
        return None

    group = await settings_cache.chat(chat_tg_id)
    tracked = group is not None and group.collect
    if tracked:
        try:
            await repo.mark_copied(
                chat_tg_id=chat_tg_id, tg_msg_id=msg_id, text=text,
                tg_user_id=author_id, author_name=author_name, date=date,
            )
        except Exception:
            # БД не должна ломать копирование
            log.exception("mark_copied упал")

    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Копировать текст", url=url)
    # Сводка приходит нажавшему в личку — любому участнику группы (решение
    # владельца): группа и так видит это обсуждение, бот лишь пересказывает его.
    if tracked:
        kb.button(text="🧵 Что обсуждали вокруг",
                  callback_data=AroundCb(chat_id=chat_tg_id, msg_id=msg_id))
    kb.adjust(1)
    return kb.as_markup()


async def _reply_with_joke(message: types.Message, text: str) -> None:
    """Вместо «Готово!» — шутка. Вес голоса на самом деле никто не меняет."""
    user = message.from_user
    _remember(PendingCopy(
        text=text, requester_id=user.id if user else 0,
        source_chat_id=message.chat.id, source_msg_id=message.message_id,
        author_id=user.id if user else None, author_name=user.full_name if user else None,
        date=message.date, at=time.monotonic(),
    ))
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Всё-таки скопировать",
              callback_data=CopyAnywayCb(chat_id=message.chat.id, msg_id=message.message_id))
    await message.reply(html.escape(next_joke()) + COPY_HINT, parse_mode="HTML",
                        reply_markup=kb.as_markup())


@router.callback_query(CopyAnywayCb.filter())
async def on_copy_anyway(cb: types.CallbackQuery, callback_data: CopyAnywayCb, bot: Bot) -> None:
    """«Я ошибся»: копируем то, что бот принял за шутку. Нажать может только автор."""
    pending = _pending.get((callback_data.chat_id, callback_data.msg_id))
    if pending is None:
        await cb.answer("Это было давно — упомяните меня с текстом ещё раз", show_alert=True)
        return
    if cb.from_user.id not in (pending.requester_id, cfg.OWNER_ID):
        await cb.answer("Эта кнопка — для того, кто просил", show_alert=True)
        return

    if cfg.COPY_MODE == "dm":
        try:
            await bot.send_message(
                pending.requester_id, f"<code>{html.escape(pending.text)}</code>",
                parse_mode="HTML",
            )
        except TelegramForbiddenError:
            await cb.answer("Напиши мне в личку /start — пришлю текст туда.", show_alert=True)
            return
        _pending.pop((callback_data.chat_id, callback_data.msg_id), None)
        await cb.answer("Отправил текст в личку")
        return

    markup = await _copy_markup(
        pending.text, pending.source_chat_id, pending.source_msg_id,
        author_id=pending.author_id, author_name=pending.author_name, date=pending.date,
    )
    if markup is None:
        await cb.answer("Упс, не получилось. Попробуйте ещё раз.", show_alert=True)
        return
    _pending.pop((callback_data.chat_id, callback_data.msg_id), None)
    await cb.answer()
    if isinstance(cb.message, types.Message):
        await cb.message.edit_text("Готово! Извините, принял за шутку.", reply_markup=markup)


DIGEST_COOLDOWN_SEC = 600
_digest_shown: dict[int, float] = {}   # группа -> когда показывали дайджест по запросу


async def _reply_with_digest(message: types.Message) -> None:
    """Упоминание без текста и без ответа на сообщение — последний дайджест группы."""
    hint = ("Напиши текст после упоминания — скопирую его, или ответь упоминанием "
            "на сообщение — скопирую то сообщение.")
    group = None
    if getattr(message.chat, "type", "") != "private":
        group = await settings_cache.chat(message.chat.id)
    if group is None or group.publish == "off":
        await message.reply(hint)
        return

    # не больше раза в 10 минут на группу: иначе упоминаниями можно засыпать чат
    now = time.monotonic()
    shown = _digest_shown.get(group.tg_id)
    if shown is not None and now - shown < DIGEST_COOLDOWN_SEC:
        await message.reply("Дайджест только что был — чуть выше ↑")
        return
    try:
        from src.digest.publish import digest_on_request

        post = await digest_on_request(group)
    except Exception:
        log.exception("Дайджест по запросу не собрался")
        post = ""
    if not post:
        await message.reply(f"Опубликованных дайджестов пока нет. {hint}")
        return
    _digest_shown[group.tg_id] = now
    await message.reply(
        post, parse_mode="HTML",
        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
    )


AROUND_CACHE_SEC = 3600
around_limit = RateLimit(limit=3, window_sec=600.0)   # на участника: модель не бесплатная
_around_cache: dict[tuple[int, int], tuple[float, str]] = {}


async def around_summary(chat_tg_id: int, msg_id: int) -> str:
    """Сводка «что обсуждали вокруг». Час помним: жмут обычно несколько человек подряд."""
    now = time.monotonic()
    cached = _around_cache.get((chat_tg_id, msg_id))
    if cached and now - cached[0] < AROUND_CACHE_SEC:
        return cached[1]
    summary = await answer_about_thread(chat_tg_id, msg_id)
    _around_cache[(chat_tg_id, msg_id)] = (now, summary)
    for key, (at, _) in list(_around_cache.items()):
        if now - at > AROUND_CACHE_SEC:
            _around_cache.pop(key, None)
    return summary


async def around_allowed(chat_tg_id: int, user_id: int) -> bool:
    """Кнопку в группе может нажать любой её участник — если группа это разрешает."""
    if user_id == cfg.OWNER_ID:
        return True
    if user_id in await settings_cache.blocked():
        return False
    group = await settings_cache.chat(chat_tg_id)
    return bool(group and group.collect and group.copier == "allow")


@router.callback_query(AroundCb.filter())
async def on_around(
    cb: types.CallbackQuery, callback_data: AroundCb, bot: Bot
) -> None:
    user_id = cb.from_user.id
    if not await around_allowed(callback_data.chat_id, user_id):
        await cb.answer("В этой группе сводки выключены", show_alert=True)
        return
    if user_id != cfg.OWNER_ID and not around_limit.allow(user_id):
        await cb.answer("Слишком часто — попробуйте через несколько минут", show_alert=True)
        return

    # Сначала — можем ли вообще написать в личку: иначе человек ждал бы впустую
    try:
        await bot.send_message(user_id, "🧵 Собираю, что обсуждали вокруг…")
    except TelegramForbiddenError:
        await cb.answer(
            "Сначала откройте личку со мной и нажмите «Старт», потом — кнопку ещё раз",
            show_alert=True,
        )
        return
    except Exception:
        log.exception("Не удалось написать %s в личку", user_id)
        await cb.answer("Не получилось, попробуйте позже", show_alert=True)
        return

    await cb.answer("Пришлю в личку")
    try:
        summary_html = await around_summary(callback_data.chat_id, callback_data.msg_id)
    except Exception:
        log.exception("answer_about_thread упал")
        summary_html = "Не получилось собрать обсуждение — попробуйте позже."
    try:
        await bot.send_message(
            user_id,
            summary_html,
            parse_mode="HTML",
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
        )
    except Exception:
        log.exception("Сводку не удалось отправить %s", user_id)
