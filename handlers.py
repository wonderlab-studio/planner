"""
handlers.py — хендлеры команд Telegram-бота.

Обрабатывает текстовые сообщения, команды со слешем
и интерактивные кнопки карточек (InlineKeyboard + ConversationHandler).

Стек: python-telegram-bot v20+ (async), loguru.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Awaitable

import db
from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from board_logic import BoardLogic, WEEKDAY_COLUMNS
from claude_client import ClaudeClient
from kaiten_client import Card, KaitenClient, TAG_IDS, TZ_MSK
from notifier import Notifier

# ── Тип для routine-коллбэков ─────────────────────────────────────────────────

RoutineCallable = Callable[[], Awaitable[str]]

# ── Состояния ConversationHandler ─────────────────────────────────────────────

CARD_ACTION          = 0  # пользователь выбрал карточку, ждём действие
AWAITING_COMMENT     = 1  # ждём текст комментария (для done или comment)
AWAITING_HOURS       = 2  # ждём число часов для «Продолжить в другой день»
AWAITING_MOVE_TARGET = 3  # ждём куда перенести
AWAITING_QUESTION    = 4  # ждём вопрос для кнопки «Совет»
AWAITING_REMINDER_TIME = 5  # ждём дату/время для кнопки «Напоминалка»
CREATE_AWAITING_TITLE         = 6   # ждём название новой задачи
CREATE_MENU                   = 7   # показан конструктор, ждём нажатие кнопки-параметра
CREATE_AWAITING_SIZE_TEXT     = 8   # ждём свой размер в часах текстом (реализация в след. задаче)
CREATE_AWAITING_EVENT_TEXT    = 9   # ждём дату/время события текстом (реализация в след. задаче)
CREATE_AWAITING_DEADLINE_TEXT = 10  # ждём дату дедлайна текстом (реализация в след. задаче)
CREATE_AWAITING_EDIT_TITLE    = 11  # ждём новое название при редактировании карточки
CREATE_AWAITING_DESCRIPTION   = 12  # ждём текст описания задачи
# CONFIRM_RISKY_MOVE — не используется как состояние диалога;
# confirm_move_cb/confirm_cancel_cb зарегистрированы как top-level хендлеры

# ── Фильтр «известные команды» — чтобы они не попадали в состояния диалога ────

_MAIN_COMMANDS_FILTER = filters.Regex(
    r"(?i)^(утро|вечер|создать|создай|готово|выполнено|сделал|сделано"
    r"|перенести|перенеси|переместить|заметка|комментарий|пересобрать|перепланируй)\b"
)

# ── Лимиты ────────────────────────────────────────────────────────────────────

_MAX_TG_LEN      = 4096   # символов в одном сообщении Telegram
MAX_CARD_BUTTONS = 20     # максимум кнопок на одной странице
_BTN_TITLE_LEN   = 40     # символов в тексте кнопки

# ── Текст подсказки ───────────────────────────────────────────────────────────

HELP_TEXT = """\
📋 *Команды планировщика:*

*Ключевые слова:*
• `утро` — план на день + кнопки карточек
• `вечер` — итог дня
• `пересобрать` / `перепланируй` — пересобрать план с текущего момента
• `/replan` — то же самое, что «пересобрать»

*Задачи:*
• `создать <описание>` — создать карточку _(можно добавить «размер - 2» или «размер 0.5» для явного размера в часах)_
• `создай <описание>` — то же самое
• `/add <описание>` — создать карточку
• `/newtask` — создать задачу через пошаговый конструктор (размер, событие, дедлайн, регулярность, важность, напоминалка)

*Управление:*
• `готово <описание>` — завершить задачу; для регулярных (ежедневно/по будням/по выходным/еженедельно) переносит на следующий цикл вместо архивации
• `/done <описание>` — то же самое
• `перенести <описание> <куда>` — переместить карточку
• `/move <описание> <куда>` — переместить карточку
• `заметка <название> // <текст>` — добавить комментарий к карточке
• `/note <название> // <текст>` — добавить комментарий

*Просмотр других колонок:*
• `/other` — посмотреть задачи не только сегодняшнего дня (другие дни недели, следующая неделя, далёкие времена)

*Кнопки над карточкой:*
• ✅ *Готово* — завершить (регулярные → следующий цикл)
• ⏭ *Продолжить в другой день* — перенести остаток на следующий подходящий день
• 💬 *Комментарий* — добавить заметку к карточке
• 📅 *Перенести* — переместить в другой день/колонку
• ✏️ *Редактировать* — изменить размер/событие/дедлайн/регулярность/важность/напоминалку через тот же конструктор, что и при создании
• 🤖 *Совет* — получить совет от AI по задаче
• 🔔 *Напоминалка* — установить время события (event_time)
• ← *Назад* — вернуться к списку карточек
_Пагинация:_ кнопки «← Назад» / «Ещё →» при длинном списке карточек

*Меню (кнопка ☰ слева от поля ввода):*
• Создать задачу — то же, что `/newtask`
• Задачи другой колонки — то же, что `/other`
• Пересобрать — то же, что «пересобрать»
• Помощь — этот текст

*Отмена:*
• `/cancel` — отмена текущего диалога

_Описание задачи может быть приблизительным — я найду нужную карточку._
_Пример заметки:_ `заметка Редактура главы 11 // Остановился на стр. 42`\
"""

# ── UserHandlerCtx ────────────────────────────────────────────────────────────


@dataclass
class UserHandlerCtx:
    """Зависимости для конкретного пользователя."""

    user_id: str
    kaiten: KaitenClient
    logic: BoardLogic
    notifier: Notifier
    morning_routine: RoutineCallable
    evening_routine: RoutineCallable
    replan_routine: RoutineCallable


# ── HandlersConfig ────────────────────────────────────────────────────────────


@dataclass
class HandlersConfig:
    """Зависимости для всех хендлеров. Передаётся при регистрации."""

    users: dict[int, UserHandlerCtx]   # telegram_chat_id → контекст пользователя
    claude: ClaudeClient               # Claude — общий для всех пользователей


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _split_text(text: str, max_len: int = _MAX_TG_LEN) -> list[str]:
    """Разбивает длинный текст на части не длиннее max_len.

    Режет по последнему переносу строки в пределах лимита,
    чтобы не рвать слова и Markdown-блоки посередине.
    """
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return parts


async def _reply_and_return(
    update: Update,
    text: str,
    parse_mode: str = ParseMode.MARKDOWN,
    silent: bool = False,
) -> list[Message]:
    """Отправляет ответ пользователю, возвращает список отправленных Message-объектов.

    Длинные тексты (> 4096 символов) автоматически разбиваются на части.
    При ошибке Markdown повторяет без форматирования.
    silent=True — отправить без звука.
    """
    assert update.message is not None
    sent: list[Message] = []
    for part in _split_text(text):
        try:
            msg = await update.message.reply_text(
                part, parse_mode=parse_mode, disable_notification=silent
            )
            sent.append(msg)
        except Exception:
            try:
                msg = await update.message.reply_text(part, disable_notification=silent)
                sent.append(msg)
            except Exception as exc:
                logger.error("_reply_and_return: не удалось отправить часть сообщения — {}", exc)
    return sent


async def _reply(
    update: Update,
    text: str,
    parse_mode: str = ParseMode.MARKDOWN,
    silent: bool = False,
) -> None:
    """Отправляет ответ пользователю.

    Длинные тексты (> 4096 символов) автоматически разбиваются на части.
    При ошибке Markdown повторяет без форматирования.
    silent=True — отправить без звука.
    """
    await _reply_and_return(update, text, parse_mode, silent)


def _strip_command_prefix(text: str, *prefixes: str) -> str:
    """Убирает команду-префикс из начала строки (без учёта регистра).

    Пример: _strip_command_prefix("создать купить молоко", "создать", "создай")
             → "купить молоко"
    """
    lower = text.strip().lower()
    for prefix in prefixes:
        if lower.startswith(prefix):
            return text.strip()[len(prefix):].strip()
    return text.strip()


def _is_control_section(sorted_cards: list[Card], target: Card) -> bool:
    """Определяет, находится ли target в секции «На контроле» — по позиции
    относительно разделителей (blocked=True) в списке, отсортированном по sort_order."""
    current: str | None = None
    for card in sorted_cards:
        if card.blocked:
            current = card.block_reason
        elif card.id == target.id:
            return current == "На контроле"
    return False


async def _load_active_cards(user_ctx: UserHandlerCtx) -> list[dict]:
    """Загружает карточки из всех активных колонок (дни недели + спец-колонки).

    Возвращает список простых словарей для передачи в claude.search_card_by_title.
    """
    active_columns = [
        col_id for name, col_id in user_ctx.logic.column_ids.items()
        if name != "Архив"
    ]

    all_cards: list[dict] = []
    for col_id in active_columns:
        col_name = next(k for k, v in user_ctx.logic.column_ids.items() if v == col_id)
        try:
            cards = await user_ctx.kaiten.get_cards(col_id)
            for card in cards:
                if card.blocked:
                    continue
                all_cards.append({
                    "id":     card.id,
                    "title":  card.title,
                    "column": col_name,
                })
        except Exception as exc:
            logger.warning(
                "_load_active_cards: ошибка при загрузке колонки {} — {}", col_id, exc
            )

    logger.debug("_load_active_cards: загружено {} карточек", len(all_cards))
    return all_cards


# ── Кнопки карточек ───────────────────────────────────────────────────────────

async def send_card_buttons(
    cards: list[Card],
    bot: Bot,
    chat_id: int | str,
    page: int = 0,
    silent: bool = False,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Отправляет страницу InlineKeyboard из карточек сегодняшнего дня.

    Карточки сортируются: «На контроле» — всегда в конец, затем по event_time.
    Пагинация: MAX_CARD_BUTTONS кнопок на страницу.
    callback_data = "card:{card.id}"
    Навигация: "← Назад" (page:{page-1}), "Ещё →" (page:{page+1}).
    silent=True — отправить без звука.
    Кнопки с event_time на сегодня отображают префикс времени «ЧЧ:ММ».
    context — если передан, удаляет предыдущее сообщение с кнопками
    (context.user_data["last_buttons_msg_id"]) и сохраняет id нового сообщения.
    """
    # Сортируем ПОЛНЫЙ список по sort_order — нужно для определения секции «На контроле»
    full_sorted = sorted(cards, key=lambda c: c.sort_order)
    task_cards = [c for c in cards if not c.blocked and not c.archived]
    if not task_cards:
        logger.debug("send_card_buttons: нет карточек для отображения")
        return

    # Удаляем предыдущее сообщение со списком кнопок-задач
    if context is not None:
        old_msg_id = context.user_data.get("last_buttons_msg_id")
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception:
                pass

    # Сортировка: «На контроле» всегда в конец, затем по event_time
    task_cards.sort(
        key=lambda c: (
            _is_control_section(full_sorted, c),
            c.event_time is None,
            c.event_time or datetime.min.replace(tzinfo=TZ_MSK),
        )
    )

    start = page * MAX_CARD_BUTTONS
    shown = task_cards[start : start + MAX_CARD_BUTTONS]
    today_msk = datetime.now(TZ_MSK).date()

    def _button_text(c: Card) -> str:
        prefix = ""
        if c.event_time and c.event_time.date() == today_msk:
            prefix = f"{c.event_time:%H:%M} "
        available = _BTN_TITLE_LEN - len(prefix)
        title = c.title[:available] + ("…" if len(c.title) > available else "")
        return prefix + title

    keyboard = [
        [InlineKeyboardButton(
            text=_button_text(c),
            callback_data=f"card:{c.id}",
        )]
        for c in shown
    ]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("← Назад", callback_data=f"page:{page - 1}"))
    if start + MAX_CARD_BUTTONS < len(task_cards):
        nav_row.append(InlineKeyboardButton("Ещё →", callback_data=f"page:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    markup = InlineKeyboardMarkup(keyboard)
    total_pages = (len(task_cards) - 1) // MAX_CARD_BUTTONS + 1
    text = f"📋 *Карточки на сегодня* (стр. {page + 1}/{total_pages}):"

    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
            disable_notification=silent,
        )
        if context is not None:
            context.user_data["last_buttons_msg_id"] = sent_msg.message_id
        logger.debug("send_card_buttons: отправлено {} кнопок, стр={}", len(shown), page)
    except Exception as exc:
        logger.error("send_card_buttons: ошибка отправки — {}", exc)


# ── Обработчики утро/вечер/пересобрать ───────────────────────────────────────

async def _handle_morning(
    update: Update,
    user_ctx: UserHandlerCtx,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Запускает утреннюю рутину, отправляет план и кнопки карточек.

    Если routine возвращает непустую строку — отвечает ею прямо в чат, затем
    откепляет все ранее закреплённые сообщения и закрепляет последнее сообщение плана.
    Если пустую — значит scheduler уже отправил через Notifier, дубль не нужен.
    После плана всегда отправляет кнопки карточек сегодняшнего дня беззвучно.
    """
    assert update.message is not None
    logger.info("handle_morning: запрос от пользователя")

    await update.message.reply_text("⏳ Составляю план дня…")
    try:
        plan_text = await user_ctx.morning_routine()
        if plan_text:
            sent_messages = await _reply_and_return(update, plan_text, silent=True)
            if sent_messages and update.effective_chat:
                try:
                    await update.effective_chat.unpin_all_messages()
                except Exception as exc:
                    logger.warning("handle_morning: unpin_all_messages error — {}", exc)
                try:
                    await sent_messages[-1].pin(disable_notification=True)
                except Exception as exc:
                    logger.warning("handle_morning: pin error — {}", exc)
    except Exception as exc:
        logger.exception("handle_morning: ошибка — {}", exc)
        await _reply(update, "⚠️ Не удалось составить план. Попробуй позже.")
        return

    # ── Кнопки карточек ───────────────────────────────────────────────────────
    if context is not None and update.effective_chat:
        context.user_data["view_ctx"] = {"scope": "today", "page": 0}
        try:
            today_col_id = user_ctx.logic.get_today_column_id()
            today_cards = await user_ctx.kaiten.get_cards(today_col_id)
            if today_cards:
                await send_card_buttons(
                    today_cards, context.bot, update.effective_chat.id,
                    page=0, silent=True, context=context,
                )
        except Exception as exc:
            logger.warning("handle_morning: не удалось отправить кнопки карточек — {}", exc)


async def _handle_evening(update: Update, user_ctx: UserHandlerCtx) -> None:
    """Запускает вечернюю рутину и отправляет итог дня.

    Если routine возвращает непустую строку — отвечает ею прямо в чат.
    Если пустую — значит scheduler уже отправил через Notifier, дубль не нужен.
    """
    assert update.message is not None
    logger.info("handle_evening: запрос от пользователя")

    await update.message.reply_text("⏳ Подвожу итоги дня…")
    try:
        summary_text = await user_ctx.evening_routine()
        if summary_text:
            await _reply(update, summary_text)
    except Exception as exc:
        logger.exception("handle_evening: ошибка — {}", exc)
        await _reply(update, "⚠️ Не удалось подвести итог. Попробуй позже.")


async def _handle_replan(
    update: Update,
    user_ctx: UserHandlerCtx,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Пересобирает расписание с текущего момента, отправляет план + pin + кнопки."""
    assert update.message is not None
    logger.info("handle_replan: запрос от пользователя")
    await update.message.reply_text("🔄 Пересобираю план с текущего момента…")
    try:
        plan_text = await user_ctx.replan_routine()
    except Exception as exc:
        logger.exception("handle_replan: ошибка — {}", exc)
        await _reply(update, "⚠️ Не удалось пересобрать план. Попробуй позже.")
        return
    if plan_text:
        sent_messages = await _reply_and_return(update, plan_text, silent=True)
        if sent_messages and update.effective_chat:
            try:
                await update.effective_chat.unpin_all_messages()
            except Exception as exc:
                logger.warning("handle_replan: unpin_all_messages error — {}", exc)
            try:
                await sent_messages[-1].pin(disable_notification=True)
            except Exception as exc:
                logger.warning("handle_replan: pin error — {}", exc)
    if context is not None and update.effective_chat:
        context.user_data["view_ctx"] = {"scope": "today", "page": 0}
        try:
            today_col_id = user_ctx.logic.get_today_column_id()
            today_cards = await user_ctx.kaiten.get_cards(today_col_id)
            if today_cards:
                await send_card_buttons(
                    today_cards, context.bot, update.effective_chat.id,
                    page=0, silent=True, context=context,
                )
        except Exception as exc:
            logger.warning("handle_replan: не удалось отправить кнопки карточек — {}", exc)


# ── Обработчик «создать» ──────────────────────────────────────────────────────

async def _handle_create(
    update: Update,
    cfg: HandlersConfig,
    raw_text: str,
    user_ctx: UserHandlerCtx,
) -> None:
    """Парсит намерение и создаёт карточку в Kaiten."""
    assert update.message is not None
    logger.info("handle_create: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(
            update, "❓ Укажи описание задачи. Например: `создать Купить молоко завтра`"
        )
        return

    try:
        intent = await cfg.claude.parse_intent(raw_text)
    except Exception as exc:
        logger.exception("handle_create: parse_intent error — {}", exc)
        await _reply(update, "⚠️ Не удалось разобрать команду. Попробуй ещё раз.")
        return

    if intent.get("action") == "unknown":
        await _reply(update, "❓ Не понял команду. Попробуй написать подробнее.")
        return

    title        = intent.get("title") or raw_text
    column_name  = intent.get("column")
    section      = intent.get("section") or "Утро"
    deadline     = intent.get("deadline")
    importance   = intent.get("importance")
    size         = intent.get("size")

    if column_name and column_name in user_ctx.logic.column_ids:
        column_id = user_ctx.logic.column_ids[column_name]
    elif deadline:
        # Колонка не задана явно, но дедлайн есть — вычисляем колонку из даты
        try:
            target_date = date.fromisoformat(deadline)
            column_id = user_ctx.logic.resolve_column_for_date(target_date)
            column_name = user_ctx.logic.column_name_by_id.get(column_id, str(column_id))
        except Exception as exc:
            logger.warning("handle_create: resolve_column_for_date({}) — {}, используем сегодня", deadline, exc)
            column_id = user_ctx.logic.get_today_column_id()
            column_name = next((k for k, v in user_ctx.logic.column_ids.items() if v == column_id), "сегодня")
    else:
        column_id = user_ctx.logic.get_today_column_id()
        column_name = next((k for k, v in user_ctx.logic.column_ids.items() if v == column_id), "сегодня")

    try:
        sort_order = await user_ctx.logic.get_section_sort_order(column_id, section)
    except Exception as exc:
        logger.warning("handle_create: get_section_sort_order error — {}, используем 1.0", exc)
        sort_order = 1.0

    properties: dict | None = None
    if importance:
        properties = user_ctx.kaiten.importance_property(importance)

    due_date_iso: str | None = f"{deadline}T00:00:00.000Z" if deadline else None

    try:
        card = await user_ctx.kaiten.create_card(
            column_id=column_id,
            title=title,
            due_date=due_date_iso,
            sort_order=sort_order,
            properties=properties,
            size=size,
        )
    except Exception as exc:
        logger.exception("handle_create: create_card error — {}", exc)
        await _reply(update, "⚠️ Не удалось создать карточку. Попробуй позже.")
        return

    if card is None:
        await _reply(update, "⚠️ Kaiten не вернул карточку. Возможно, создание не удалось.")
        return

    # B.9: логируем создание карточки
    await _log_event(user_ctx, "created", card.title, detail=f"{column_name} / {section}")

    parts = [f"✅ Карточка создана: *{card.title}*", f"📅 Колонка: {column_name} / {section}"]
    if deadline:
        parts.append(f"⏰ Дедлайн: {deadline}")
    if importance:
        parts.append(f"🔥 Важность: {importance}")
    if size is not None:
        parts.append(f"⏱ Размер: {size} ч")
    await _reply(update, "\n".join(parts))

    # Предлагаем пересобрать план, если дедлайн — сегодня
    today_iso = datetime.now(TZ_MSK).date().isoformat()
    if deadline == today_iso:
        await update.message.reply_text(
            "📅 Задача с дедлайном на сегодня. Пересобрать план?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, пересобрать", callback_data="replan_offer:yes"),
                InlineKeyboardButton("❌ Нет", callback_data="replan_offer:no"),
            ]]),
        )


# ── Обработчик «готово» ───────────────────────────────────────────────────────

async def _handle_done(
    update: Update,
    cfg: HandlersConfig,
    raw_text: str,
    user_ctx: UserHandlerCtx,
) -> None:
    """Ищет карточку и завершает её.

    Для регулярных задач (ежедневно/по будням/по выходным/еженедельно) вместо архивации
    переносит на следующий подходящий день по тегу — чтобы не ломать ротацию.
    Для обычных задач — архивирует.
    """
    assert update.message is not None
    logger.info("handle_done: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(update, "❓ Укажи название задачи. Например: `готово купить молоко`")
        return

    await update.message.reply_text("🔍 Ищу карточку…")

    try:
        all_cards = await _load_active_cards(user_ctx)
        matched = await cfg.claude.search_card_by_title(raw_text, all_cards)
    except Exception as exc:
        logger.exception("handle_done: поиск карточки — {}", exc)
        await _reply(update, "⚠️ Ошибка при поиске карточки. Попробуй позже.")
        return

    if matched is None:
        await _reply(update, f"❓ Не нашёл карточку по запросу «{raw_text}».")
        return

    # B.4: для регулярной задачи override event_type="done", для архива — log после archive_card
    try:
        card = await user_ctx.kaiten.get_card(matched["id"])
        if card and user_ctx.logic.is_regular_task(card):
            # Регулярная задача: переносим на следующий цикл — это ЗАВЕРШЕНИЕ, не перенос
            ok, msg = await _postpone_card(user_ctx, card, hours=None, event_type="done")
            await _reply(update, msg)
        else:
            ok = await user_ctx.logic.archive_card(matched["id"])
            if ok:
                await _log_event(user_ctx, "done", matched["title"])
                await _reply(update, f"✅ Готово! Карточка «{matched['title']}» перемещена в архив.")
            else:
                await _reply(update, f"⚠️ Не удалось архивировать «{matched['title']}».")
    except Exception as exc:
        logger.exception("handle_done: error — {}", exc)
        await _reply(update, "⚠️ Ошибка при завершении задачи. Попробуй позже.")


# ── Обработчик «перенести» ────────────────────────────────────────────────────

async def _handle_move(
    update: Update,
    cfg: HandlersConfig,
    raw_text: str,
    user_ctx: UserHandlerCtx,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Ищет карточку и перемещает её в нужную колонку/секцию."""
    assert update.message is not None
    logger.info("handle_move: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(
            update,
            "❓ Укажи задачу и куда перенести. Например: `перенести молоко на завтра`",
        )
        return

    try:
        intent = await cfg.claude.parse_intent(raw_text)
    except Exception as exc:
        logger.exception("handle_move: parse_intent error — {}", exc)
        await _reply(update, "⚠️ Не удалось разобрать команду.")
        return

    query        = intent.get("title") or raw_text
    column_name  = intent.get("column")
    deadline     = intent.get("deadline")
    section      = intent.get("section") or "Утро"

    await update.message.reply_text("🔍 Ищу карточку…")

    try:
        all_cards = await _load_active_cards(user_ctx)
        matched = await cfg.claude.search_card_by_title(query, all_cards)
    except Exception as exc:
        logger.exception("handle_move: поиск — {}", exc)
        await _reply(update, "⚠️ Ошибка при поиске карточки.")
        return

    if matched is None:
        await _reply(update, f"❓ Не нашёл карточку по запросу «{query}».")
        return

    target_column_id: int | None = None
    target_column_name: str | None = None

    if deadline:
        try:
            target_date = date.fromisoformat(deadline)
            target_column_id = user_ctx.logic.resolve_column_for_date(target_date)
            target_column_name = user_ctx.logic.column_name_by_id.get(
                target_column_id, str(target_column_id)
            )
        except ValueError:
            logger.warning("_handle_move: не удалось разобрать deadline={!r}", deadline)

    if target_column_id is None:
        if column_name and column_name in user_ctx.logic.column_ids:
            target_column_id   = user_ctx.logic.column_ids[column_name]
            target_column_name = column_name
        else:
            tomorrow_wd        = (datetime.now(TZ_MSK).date() + timedelta(days=1)).weekday()
            target_column_name = WEEKDAY_COLUMNS[tomorrow_wd]
            target_column_id   = user_ctx.logic.column_ids[target_column_name]

    # Мягкое сопротивление: критическая задача с дедлайном сегодня/завтра
    try:
        card_obj = await user_ctx.kaiten.get_card(matched["id"])
    except Exception:
        card_obj = None
    today = datetime.now(TZ_MSK).date()
    tomorrow = today + timedelta(days=1)
    due_dt = card_obj.due_date_parsed if card_obj else None
    due_date = due_dt.date() if due_dt else None
    risky = bool(card_obj) and card_obj.importance == "критическое" and due_date in (today, tomorrow)

    # Перенос в «На контроле» — это не откладывание задачи, поэтому для него risky-
    # подтверждение не запрашивается даже у критической задачи с близким дедлайном.
    # Вместо этого после переноса запускается отдельный диалог о дедлайне (ниже).
    if risky and section != "На контроле" and context is not None:
        context.user_data["pending_move"] = {
            "kind": "move",
            "card_id": matched["id"],
            "title": matched["title"],
            "target_col_id": target_column_id,
            "target_col_name": target_column_name,
            "section": section,
        }
        await _reply(
            update,
            f"⚠️ «{matched['title']}» — критическая задача с дедлайном {due_date.isoformat()}.\n"
            f"Точно перенести на {target_column_name}?",
        )
        await update.message.reply_text(
            "Подтверди:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Да, перенести", callback_data="confirm:move"),
                InlineKeyboardButton("Отмена", callback_data="confirm:cancel"),
            ]]),
        )
        return

    try:
        sort_order = await user_ctx.logic.get_section_sort_order(target_column_id, section)
    except Exception as exc:
        logger.warning("handle_move: get_section_sort_order — {}, используем 1.0", exc)
        sort_order = 1.0

    try:
        card = await user_ctx.kaiten.move_card(matched["id"], target_column_id, sort_order)
    except Exception as exc:
        logger.exception("handle_move: move_card error — {}", exc)
        await _reply(update, "⚠️ Не удалось переместить карточку.")
        return

    # B.8: логируем перенос (путь без risky-подтверждения)
    if card:
        await _log_event(
            user_ctx, "moved", matched["title"], detail=f"{target_column_name} / {section}"
        )
        await _reply(
            update,
            f"📦 «{matched['title']}» перенесена → *{target_column_name} / {section}*",
        )
        if section == "На контроле":
            try:
                today_str = datetime.now(TZ_MSK).date().isoformat()
                await user_ctx.kaiten.add_comment(
                    matched["id"], f"Задача переведена на контроль {today_str}"
                )
            except Exception as exc:
                logger.warning(
                    "_handle_move: не удалось добавить комментарий о контроле — {}", exc
                )
            # Для критической задачи с близким дедлайном — предлагаем перенести дедлайн
            # вместо risky-подтверждения (которое было пропущено выше для «На контроле»).
            if risky and context is not None:
                context.user_data["pending_deadline_reschedule"] = {
                    "card_id": matched["id"],
                    "title": matched["title"],
                }
                await update.message.reply_text(
                    "На какой день перенести дедлайн этой задачи?\n"
                    "_«.» — оставить без изменений, «отменить» — убрать дедлайн, или напиши дату_",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
    else:
        await _reply(update, f"⚠️ Не удалось переместить «{matched['title']}».")


# ── Обработчик «заметка» ──────────────────────────────────────────────────────

async def _handle_note(
    update: Update,
    cfg: HandlersConfig,
    raw_text: str,
    user_ctx: UserHandlerCtx,
) -> None:
    """Ищет карточку и добавляет к ней комментарий.

    Формат: «Название карточки // Текст заметки»
    Левая часть (до //) — поисковый запрос, правая — текст комментария.
    Если разделитель отсутствует — просим пользователя уточнить формат.
    """
    assert update.message is not None
    logger.info("handle_note: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(
            update,
            "❓ Укажи задачу и текст заметки через `//`.\n"
            "Пример: `заметка Редактура главы 11 // Остановился на стр. 42`",
        )
        return

    if "//" not in raw_text:
        await _reply(
            update,
            "❓ Не нашёл разделитель `//`.\n"
            "Формат: `заметка *Название карточки* // *Текст заметки*`\n"
            "Пример: `заметка Редактура главы 11 // Остановился на стр. 42`",
        )
        return

    note_parts = raw_text.split("//", maxsplit=1)
    query     = note_parts[0].strip()
    note_text = note_parts[1].strip()

    if not query:
        await _reply(update, "❓ Укажи название карточки перед `//`.")
        return
    if not note_text:
        await _reply(update, "❓ Укажи текст заметки после `//`.")
        return

    logger.debug("handle_note: query={!r} note={!r}", query, note_text)
    await update.message.reply_text("🔍 Ищу карточку…")

    try:
        all_cards = await _load_active_cards(user_ctx)
        matched = await cfg.claude.search_card_by_title(query, all_cards)
    except Exception as exc:
        logger.exception("handle_note: поиск — {}", exc)
        await _reply(update, "⚠️ Ошибка при поиске карточки.")
        return

    if matched is None:
        await _reply(update, f"❓ Не нашёл карточку по запросу «{query}».")
        return

    try:
        ok = await user_ctx.kaiten.add_comment(matched["id"], note_text)
    except Exception as exc:
        logger.exception("handle_note: add_comment error — {}", exc)
        await _reply(update, "⚠️ Не удалось добавить заметку.")
        return

    if ok:
        await _reply(update, f"📝 Заметка добавлена к «{matched['title']}».")
    else:
        await _reply(update, f"⚠️ Не удалось добавить заметку к «{matched['title']}».")


# ── Вспомогательные функции ConversationHandler ───────────────────────────────

def _action_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура действий над карточкой."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Готово",                   callback_data="action:done")],
        [InlineKeyboardButton("⏭ Продолжить в другой день", callback_data="action:today")],
        [InlineKeyboardButton("💬 Комментарий",              callback_data="action:comment")],
        [InlineKeyboardButton("📅 Перенести",                callback_data="action:move")],
        [InlineKeyboardButton("🤖 Совет",                    callback_data="action:advice")],
        [InlineKeyboardButton("🔔 Напоминалка",              callback_data="action:reminder")],
        [InlineKeyboardButton("✏️ Редактировать",            callback_data="action:edit")],
        [InlineKeyboardButton("📄 Описание и комментарии",   callback_data="action:description")],
        [InlineKeyboardButton("← Назад",                     callback_data="action:back")],
    ])


def _render_task_constructor(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Строит текст и клавиатуру карточки-конструктора из context.user_data['new_task'].

    Переиспользуется и для создания, и (в будущем) для редактирования задачи.
    """
    size = data.get("size")
    if size is None:
        size_label = "не указан (15 мин по умолчанию)"
    elif size == 999:
        size_label = "от 1 часа (сколько влезет)"
    else:
        size_label = f"{size} ч"

    if data.get("event_date") and data.get("event_time"):
        event_label = f"{data['event_date']} в {data['event_time'][:5]}"
    else:
        event_label = "не указано"

    deadline_label = data.get("deadline") or "не указан"

    reg = data.get("regularity")
    if reg == "еженедельно" and data.get("weekday"):
        reg_label = f"еженедельно ({data['weekday']})"
    else:
        reg_label = reg or "разовая"

    importance_label = data.get("importance") or "обычная"
    reminder_label = "вкл" if data.get("reminder") else "выкл"

    _raw_desc = (data.get("description") or "").strip()
    if len(_raw_desc) > 80:
        description_label = _raw_desc[:80] + "…"
    elif _raw_desc:
        description_label = _raw_desc
    else:
        description_label = "—"

    title = data.get("title") or ""
    if data.get("_edit_card_id"):
        header = f"✏️ Редактирование: «{title}»"
    else:
        header = f"🆕 Новая задача: «{title}»"

    text = (
        f"{header}\n\n"
        f"📏 Размер: {size_label}\n"
        f"📅 Событие: {event_label}\n"
        f"⏰ Дедлайн: {deadline_label}\n"
        f"🔁 Регулярность: {reg_label}\n"
        f"🔥 Важность: {importance_label}\n"
        f"🔔 Напоминалка: {reminder_label}\n"
        f"📄 Описание: {description_label}"
    )

    keyboard = [
        [InlineKeyboardButton("📏 Размер", callback_data="nt:size"),
         InlineKeyboardButton("📅 Событие", callback_data="nt:event")],
        [InlineKeyboardButton("⏰ Дедлайн", callback_data="nt:deadline"),
         InlineKeyboardButton("🔁 Регулярность", callback_data="nt:regularity")],
        [InlineKeyboardButton("🔥 Важность", callback_data="nt:importance"),
         InlineKeyboardButton("🔔 Напоминалка", callback_data="nt:reminder")],
        [InlineKeyboardButton("📄 Описание", callback_data="nt:description")],
    ]
    if data.get("_edit_card_id"):
        keyboard.append([InlineKeyboardButton("📝 Название", callback_data="nt:title")])
    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="nt:done"),
                     InlineKeyboardButton("❌ Отмена", callback_data="nt:cancel")])
    return text, InlineKeyboardMarkup(keyboard)


async def _finalize_new_task(
    query, context: ContextTypes.DEFAULT_TYPE, user_ctx: "UserHandlerCtx", data: dict,
) -> int:
    """Создаёт карточку в Kaiten по данным конструктора (context.user_data['new_task'])."""
    title = data.get("title") or "Без названия"

    if data.get("event_date"):
        target_date = date.fromisoformat(data["event_date"])
        column_id = user_ctx.logic.resolve_column_for_date(target_date)
        hour = int(data["event_time"][:2]) if data.get("event_time") else 9
        section = "Утро" if hour < 12 else ("День" if hour < 19 else "Вечер")
    elif data.get("deadline"):
        target_date = date.fromisoformat(data["deadline"])
        column_id = user_ctx.logic.resolve_column_for_date(target_date)
        section = "Утро"
    else:
        regularity = data.get("regularity")
        today_msk = datetime.now(TZ_MSK).date()
        today_wd = today_msk.weekday()  # 0=Пн … 6=Вс

        if regularity == "еженедельно":
            wd_short = data.get("weekday")
            _wd_to_col: dict[str, str] = {
                "ПН": "Понедельник", "ВТ": "Вторник", "СР": "Среда",
                "ЧТ": "Четверг", "ПТ": "Пятница", "СБ": "Суббота", "ВС": "Воскресенье",
            }
            full_name = _wd_to_col.get(wd_short) if wd_short else None
            if full_name:
                try:
                    column_id = user_ctx.logic.get_column_id(full_name)
                except ValueError:
                    logger.warning(
                        "_finalize_new_task: неизвестная колонка {!r}, fallback на сегодня",
                        full_name,
                    )
                    column_id = user_ctx.logic.get_today_column_id()
            else:
                column_id = user_ctx.logic.get_today_column_id()
        elif regularity == "по выходным" and today_wd <= 4:
            # Сегодня будний день → первые выходные начинаются с субботы
            try:
                column_id = user_ctx.logic.get_column_id("Суббота")
            except ValueError:
                logger.warning("_finalize_new_task: колонка «Суббота» не найдена, fallback на сегодня")
                column_id = user_ctx.logic.get_today_column_id()
        elif regularity == "по будням" and today_wd >= 5:
            # Сегодня выходной → ближайший будний день в следующей неделе
            try:
                column_id = user_ctx.logic.get_column_id("Следующая неделя")
            except ValueError:
                logger.warning(
                    "_finalize_new_task: колонка «Следующая неделя» не найдена, fallback на сегодня"
                )
                column_id = user_ctx.logic.get_today_column_id()
        else:
            # "ежедневно", "по будням" в будни, "по выходным" в выходные, или разовая
            column_id = user_ctx.logic.get_today_column_id()
        section = "Утро"
    column_name = user_ctx.logic.column_name_by_id.get(column_id, str(column_id))

    try:
        sort_order = await user_ctx.logic.get_section_sort_order(column_id, section)
    except Exception as exc:
        logger.warning("_finalize_new_task: get_section_sort_order — {}, используем 1.0", exc)
        sort_order = 1.0

    properties: dict = {}
    if data.get("event_date") and data.get("event_time"):
        try:
            event_dt = datetime.fromisoformat(
                f"{data['event_date']}T{data['event_time']}"
            ).replace(tzinfo=TZ_MSK)
            properties.update(user_ctx.kaiten.event_time_property(event_dt))
        except Exception as exc:
            logger.warning("_finalize_new_task: event_time_property — {}", exc)
    if data.get("importance"):
        imp_props = user_ctx.kaiten.importance_property(data["importance"])
        if imp_props:
            properties.update(imp_props)
    if data.get("regularity") == "еженедельно" and data.get("weekday"):
        wd_props = user_ctx.kaiten.weekday_property(data["weekday"])
        if wd_props:
            properties.update(wd_props)

    due_date_iso = f"{data['deadline']}T00:00:00.000Z" if data.get("deadline") else None

    try:
        card = await user_ctx.kaiten.create_card(
            column_id=column_id,
            title=title,
            due_date=due_date_iso,
            sort_order=sort_order,
            properties=properties or None,
            size=data.get("size"),
            description=data.get("description"),
        )
    except Exception as exc:
        logger.exception("_finalize_new_task: create_card error — {}", exc)
        await query.edit_message_text("⚠️ Не удалось создать карточку. Попробуй позже.")
        context.user_data.pop("new_task", None)
        return ConversationHandler.END

    if card is None:
        await query.edit_message_text("⚠️ Kaiten не вернул карточку. Возможно, создание не удалось.")
        context.user_data.pop("new_task", None)
        return ConversationHandler.END

    tag_names: list[str] = []
    if data.get("regularity"):
        tag_names.append(data["regularity"])
    if data.get("reminder"):
        tag_names.append("напомнить")
    today_iso = datetime.now(TZ_MSK).date().isoformat()
    is_hard_event_today = data.get("event_date") == today_iso and data.get("event_time") is not None
    if is_hard_event_today:
        tag_names.append("жёсткое событие")
    for tag_name in tag_names:
        try:
            await user_ctx.kaiten.add_tag_by_name(card.id, tag_name)
        except Exception as exc:
            logger.warning("_finalize_new_task: add_tag_by_name({}) — {}", tag_name, exc)

    await _log_event(user_ctx, "created", card.title, detail=f"{column_name} / {section}")

    parts = [f"✅ Карточка создана: *{card.title}*", f"📅 Колонка: {column_name} / {section}"]
    if data.get("deadline"):
        parts.append(f"⏰ Дедлайн: {data['deadline']}")
    if data.get("importance"):
        parts.append(f"🔥 Важность: {data['importance']}")
    if data.get("size") is not None:
        parts.append(f"⏱ Размер: {data['size']} ч")
    if data.get("regularity"):
        parts.append(f"🔁 Регулярность: {data['regularity']}")
    if data.get("reminder"):
        parts.append("🔔 Напоминалка включена")
    await query.edit_message_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)

    # Предлагаем пересобрать план, если задача на сегодня (событие с временем или дедлайн)
    needs_replan_offer = is_hard_event_today or data.get("deadline") == today_iso
    if needs_replan_offer:
        warning = (
            "⚠️ Задача на сегодня с конкретным временем. Пересобрать план, чтобы её учесть?"
            if is_hard_event_today
            else "📅 Задача с дедлайном на сегодня. Пересобрать план?"
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=warning,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, пересобрать", callback_data="replan_offer:yes"),
                InlineKeyboardButton("❌ Нет", callback_data="replan_offer:no"),
            ]]),
        )

    context.user_data.pop("new_task", None)
    return ConversationHandler.END


async def _finalize_edit_task(
    query, context: ContextTypes.DEFAULT_TYPE, user_ctx: "UserHandlerCtx", data: dict,
) -> int:
    """Сохраняет изменения существующей карточки (режим редактирования конструктора).

    Дедлайн меняется на месте (без переноса колонки). Событие при изменении даты
    переносит карточку в колонку/секцию, соответствующую новой дате события.
    """
    card_id = data.get("_edit_card_id")
    original = data.get("_original", {})
    title = data.get("title") or "Без названия"

    update_fields: dict = {"title": title, "size": data.get("size")}
    update_fields["due_date"] = f"{data['deadline']}T00:00:00.000Z" if data.get("deadline") else None
    update_fields["description"] = data.get("description")

    properties: dict = {}
    if data.get("event_date") and data.get("event_time"):
        try:
            event_dt = datetime.fromisoformat(
                f"{data['event_date']}T{data['event_time']}"
            ).replace(tzinfo=TZ_MSK)
            properties.update(user_ctx.kaiten.event_time_property(event_dt))
        except Exception as exc:
            logger.warning("_finalize_edit_task: event_time_property — {}", exc)
    if data.get("importance"):
        imp_props = user_ctx.kaiten.importance_property(data["importance"])
        if imp_props:
            properties.update(imp_props)
    if data.get("regularity") == "еженедельно" and data.get("weekday"):
        wd_props = user_ctx.kaiten.weekday_property(data["weekday"])
        if wd_props:
            properties.update(wd_props)
    if properties:
        update_fields["properties"] = properties

    try:
        await user_ctx.kaiten.update_card(card_id, **update_fields)
    except Exception as exc:
        logger.exception("_finalize_edit_task: update_card error — {}", exc)
        await query.edit_message_text("⚠️ Не удалось сохранить изменения. Попробуй позже.")
        context.user_data.pop("new_task", None)
        return ConversationHandler.END

    # Регулярность: снять старый тег, поставить новый, если изменилась
    old_reg = original.get("regularity")
    new_reg = data.get("regularity")
    if old_reg != new_reg:
        if old_reg:
            try:
                await user_ctx.kaiten.remove_tag_by_name(card_id, old_reg)
            except Exception as exc:
                logger.warning("_finalize_edit_task: remove_tag_by_name({}) — {}", old_reg, exc)
        if new_reg:
            try:
                await user_ctx.kaiten.add_tag_by_name(card_id, new_reg)
            except Exception as exc:
                logger.warning("_finalize_edit_task: add_tag_by_name({}) — {}", new_reg, exc)

    # Напоминалка: тег «напомнить»
    old_reminder = original.get("reminder", False)
    new_reminder = data.get("reminder", False)
    if old_reminder != new_reminder:
        try:
            if new_reminder:
                await user_ctx.kaiten.add_tag_by_name(card_id, "напомнить")
            else:
                await user_ctx.kaiten.remove_tag_by_name(card_id, "напомнить")
        except Exception as exc:
            logger.warning("_finalize_edit_task: тег напомнить — {}", exc)

    # Событие изменилось на другую дату → переносим карточку
    moved_note = ""
    old_event_date = original.get("event_date")
    new_event_date = data.get("event_date")
    if new_event_date and new_event_date != old_event_date:
        try:
            target_date = date.fromisoformat(new_event_date)
            column_id = user_ctx.logic.resolve_column_for_date(target_date)
            hour = int(data["event_time"][:2]) if data.get("event_time") else 9
            section = "Утро" if hour < 12 else ("День" if hour < 19 else "Вечер")
            sort_order = await user_ctx.logic.get_section_sort_order(column_id, section)
            await user_ctx.kaiten.move_card(card_id, column_id, sort_order)
            column_name = user_ctx.logic.column_name_by_id.get(column_id, str(column_id))
            moved_note = f"\n📅 Перенесена → {column_name} / {section}"
            await _log_event(user_ctx, "moved", title, detail=f"{column_name} / {section}")
        except Exception as exc:
            logger.warning("_finalize_edit_task: move_card — {}", exc)
            moved_note = "\n⚠️ Не удалось перенести карточку в новую колонку."

    await query.edit_message_text(
        f"✅ Изменения сохранены: *{title}*{moved_note}", parse_mode=ParseMode.MARKDOWN
    )
    context.user_data.pop("new_task", None)
    return ConversationHandler.END


# ── Просмотр других колонок (/other) ─────────────────────────────────────────

_WEEKDAY_SHORT: dict[str, str] = {
    "Понедельник": "Пн", "Вторник": "Вт", "Среда": "Ср", "Четверг": "Чт",
    "Пятница": "Пт", "Суббота": "Сб", "Воскресенье": "Вс",
}

_OC_SCOPE_LABELS: dict[str, str] = {
    "today":    "Сегодня",
    "otherdays": "Другие дни недели",
    "nextweek": "Следующая неделя",
    "faraway":  "Далёкие времена",
}


async def _collect_other_column_cards(
    user_ctx: "UserHandlerCtx", scope: str,
) -> tuple[list[tuple[Card, str | None, bool]], list[Card]]:
    """Возвращает (items, full_sorted_today) для заданной области просмотра.

    items: [(card, suffix_или_None, show_time), ...] — только незаблокированные карточки.
    full_sorted_today: все карточки сегодняшней колонки (включая заблокированные-разделители),
        отсортированные по sort_order — только для scope=="today", иначе пустой список.
        Нужен для определения секции «На контроле» через _is_control_section.
    show_time=True только если event_time карточки приходится РОВНО на дату той колонки,
    в которой карточка сейчас показана.
    """
    today = datetime.now(TZ_MSK).date()
    result: list[tuple[Card, str | None, bool]] = []

    if scope == "today":
        col_id = user_ctx.logic.get_today_column_id()
        cards = await user_ctx.kaiten.get_cards(col_id)
        full_sorted_today = sorted(cards, key=lambda c: c.sort_order)
        for c in cards:
            if c.blocked or c.archived:
                continue
            show_time = bool(c.event_time and c.event_time.date() == today)
            result.append((c, None, show_time))
        return result, full_sorted_today

    elif scope == "otherdays":
        today_name = WEEKDAY_COLUMNS[today.weekday()]
        monday = today - timedelta(days=today.weekday())
        for idx, name in enumerate(WEEKDAY_COLUMNS):
            if name == today_name:
                continue
            col_id = user_ctx.logic.column_ids.get(name)
            if col_id is None:
                continue
            col_date = monday + timedelta(days=idx)
            try:
                cards = await user_ctx.kaiten.get_cards(col_id)
            except Exception as exc:
                logger.warning("_collect_other_column_cards: колонка {} — {}", name, exc)
                continue
            short_name = _WEEKDAY_SHORT.get(name, name)
            for c in cards:
                if c.blocked or c.archived:
                    continue
                show_time = bool(c.event_time and c.event_time.date() == col_date)
                result.append((c, short_name, show_time))

    elif scope in ("nextweek", "faraway"):
        col_name = "Следующая неделя" if scope == "nextweek" else "Далекие времена"
        col_id = user_ctx.logic.column_ids.get(col_name)
        cards = await user_ctx.kaiten.get_cards(col_id) if col_id else []
        for c in cards:
            if c.blocked or c.archived:
                continue
            result.append((c, None, False))

    return result, []


async def _render_other_columns_page(
    user_ctx: "UserHandlerCtx",
    scope: str,
    page: int,
    bot,
    chat_id: int | str,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Рендерит страницу списка карточек для области просмотра scope.

    context — если передан, удаляет предыдущее сообщение со списком кнопок
    (context.user_data["last_buttons_msg_id"]) и сохраняет id нового сообщения.
    Для scope=="today" применяет ту же сортировку, что и send_card_buttons:
    секция «На контроле» — в конец списка.
    """
    items, full_sorted_today = await _collect_other_column_cards(user_ctx, scope)
    if not items:
        await bot.send_message(chat_id=chat_id, text="Пусто — карточек не найдено.")
        return

    # Удаляем предыдущее сообщение со списком кнопок-задач
    if context is not None:
        old_msg_id = context.user_data.get("last_buttons_msg_id")
        if old_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception:
                pass

    if scope == "today":
        # Та же сортировка, что в send_card_buttons: «На контроле» в конец
        items.sort(
            key=lambda t: (
                _is_control_section(full_sorted_today, t[0]),
                t[0].event_time is None,
                t[0].event_time or datetime.min.replace(tzinfo=TZ_MSK),
            )
        )
    else:
        items.sort(
            key=lambda t: (
                t[0].event_time is None,
                t[0].event_time or datetime.min.replace(tzinfo=TZ_MSK),
            )
        )

    start = page * MAX_CARD_BUTTONS
    shown = items[start : start + MAX_CARD_BUTTONS]

    def _btn_text(card: Card, suffix: str | None, show_time: bool) -> str:
        prefix = f"{card.event_time:%H:%M} " if (show_time and card.event_time) else ""
        tail = f" ({suffix})" if suffix else ""
        available = _BTN_TITLE_LEN - len(prefix) - len(tail)
        available = max(available, 5)
        title = card.title[:available] + ("…" if len(card.title) > available else "")
        return prefix + title + tail

    keyboard = [
        [InlineKeyboardButton(_btn_text(c, s, t), callback_data=f"card:{c.id}")]
        for c, s, t in shown
    ]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("← Назад", callback_data=f"oc:{scope}:{page - 1}"))
    if start + MAX_CARD_BUTTONS < len(items):
        nav_row.append(InlineKeyboardButton("Ещё →", callback_data=f"oc:{scope}:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    label = _OC_SCOPE_LABELS.get(scope, scope)
    total_pages = (len(items) - 1) // MAX_CARD_BUTTONS + 1
    text = f"📋 *{label}* (стр. {page + 1}/{total_pages}):"

    sent_msg = None
    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        try:
            sent_msg = await bot.send_message(
                chat_id=chat_id, text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as exc:
            logger.error("_render_other_columns_page: ошибка отправки — {}", exc)

    if sent_msg is not None and context is not None:
        context.user_data["last_buttons_msg_id"] = sent_msg.message_id


async def _resend_card_buttons(
    user_ctx: UserHandlerCtx,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
) -> None:
    """Повторно отправляет кнопки карточек в той же области просмотра, что была до действия.

    Читает context.user_data["view_ctx"] (scope + page), установленный при последнем
    вызове утра/пересборки/навигации. По умолчанию — scope="today", page=0.
    Для scope="today" использует send_card_buttons, для остальных — _render_other_columns_page.
    view_ctx НЕ перезаписывается — пользователь остаётся в той области, откуда открыл карточку.
    """
    # Удаляем сообщение с описанием карточки если осталось открытым
    old_desc_msg_id = context.user_data.pop("last_description_msg_id", None)
    if old_desc_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_desc_msg_id)
        except Exception:
            pass

    view_ctx = context.user_data.get("view_ctx", {"scope": "today", "page": 0})
    scope = view_ctx.get("scope", "today")
    page = view_ctx.get("page", 0)
    try:
        if scope == "today":
            today_col_id = user_ctx.logic.get_today_column_id()
            cards = await user_ctx.kaiten.get_cards(today_col_id)
            task_cards = [c for c in cards if not c.blocked and not c.archived]
            if task_cards:
                await send_card_buttons(task_cards, context.bot, chat_id, page=page, context=context)
            else:
                # Удаляем старое сообщение с кнопками перед отправкой статуса «все обработаны»
                old_msg_id = context.user_data.get("last_buttons_msg_id")
                if old_msg_id:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
                    except Exception:
                        pass
                    context.user_data.pop("last_buttons_msg_id", None)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="✅ Все карточки на сегодня обработаны.",
                )
        else:
            await _render_other_columns_page(user_ctx, scope, page, context.bot, chat_id, context=context)
    except Exception as exc:
        logger.warning("_resend_card_buttons: ошибка — {}", exc)


# ── Вспомогательная логика для кнопки «Продолжить в другой день» ─────────────

def _next_col_for_regular(card: Card, today: date, column_ids: dict[str, int]) -> int:
    """Возвращает column_id следующего подходящего дня для регулярной задачи."""
    tags = set(card.tag_ids)

    if TAG_IDS["еженедельно"] in tags:
        return column_ids["Следующая неделя"]

    for days_ahead in range(1, 8):
        candidate = today + timedelta(days=days_ahead)
        wd = candidate.weekday()

        if TAG_IDS["ежедневно"] in tags:
            break
        if TAG_IDS["по будням"] in tags and wd <= 4:
            break
        if TAG_IDS["по выходным"] in tags and wd >= 5:
            break
    else:
        logger.warning("_next_col_for_regular: не нашли слот за 7 дней, card_id={}", card.id)
        return column_ids["Следующая неделя"]

    target_wd = (today + timedelta(days=days_ahead)).weekday()
    return column_ids[WEEKDAY_COLUMNS[target_wd]]


# B.2: helper для логирования событий дня (для вечернего итога)

async def _log_event(
    user_ctx: UserHandlerCtx, event_type: str, card_title: str, detail: str = ""
) -> None:
    """Best-effort запись события дня в SQLite (для вечернего итога).

    Ошибка не критична — не должна прерывать основное действие пользователя.
    event_type: "created" | "done" | "moved".
    """
    try:
        loop = asyncio.get_event_loop()
        today = datetime.now(TZ_MSK).date()
        await loop.run_in_executor(
            None, db.append_daily_event, user_ctx.user_id, today, event_type, card_title, detail
        )
    except Exception as exc:
        logger.warning("_log_event: не удалось записать событие ({}) — {}", event_type, exc)


# B.3: _postpone_card с параметром event_type и логированием успешного переноса

async def _postpone_card(
    user_ctx: UserHandlerCtx,
    card: Card,
    hours: float | None,
    *,
    event_type: str = "moved",
) -> tuple[bool, str]:
    """Переносит карточку на следующий подходящий слот. Если hours задан — обновляет size.

    Возвращает (успех, текст_результата_для_пользователя).
    Для регулярных задач находит следующий день по тегу.
    Для обычных задач — берёт завтра.
    event_type: "moved" (дефолт) для «Продолжить в другой день»,
                "done" для завершения регулярной задачи через _handle_done / received_comment_cb.
    """
    card_id = card.id
    if hours is not None:
        try:
            await user_ctx.kaiten.update_card(card_id, size=hours)
        except Exception as exc:
            logger.exception("_postpone_card: update_card error — {}", exc)
            return False, "⚠️ Не удалось обновить размер задачи."
    try:
        card = await user_ctx.kaiten.get_card(card_id)
        if card is None:
            raise ValueError(f"card {card_id} not found after update")
        if user_ctx.logic.is_regular_task(card):
            start_col_id = _next_col_for_regular(
                card, datetime.now(TZ_MSK).date(), user_ctx.logic.column_ids
            )
        else:
            tomorrow = datetime.now(TZ_MSK).date() + timedelta(days=1)
            start_col_id = user_ctx.logic.column_ids[WEEKDAY_COLUMNS[tomorrow.weekday()]]
        slot = await user_ctx.logic.find_slot_for_card(card, start_col_id)
        if slot is None:
            slot = (user_ctx.logic.column_ids["Следующая неделя"], "Утро")
        target_col_id, target_section = slot
        sort_order = await user_ctx.logic.get_section_sort_order(target_col_id, target_section)
        await user_ctx.kaiten.move_card(card_id, target_col_id, sort_order)
        target_col_name = user_ctx.logic.column_name_by_id.get(target_col_id, str(target_col_id))
        hours_part = f": размер {hours} ч" if hours is not None else ""
        await _log_event(
            user_ctx, event_type, card.title, detail=f"{target_col_name} / {target_section}"
        )
        return True, f"✅ «{card.title}»{hours_part} → *{target_col_name} / {target_section}*"
    except Exception as exc:
        logger.exception("_postpone_card: move error — {}", exc)
        suffix = " ⚠️ Не удалось перенести карточку." if hours is not None else ""
        return False, f"⚠️ Ошибка при переносе.{suffix}"


# ── Фабрика хендлеров ─────────────────────────────────────────────────────────

def build_handlers(cfg: HandlersConfig) -> Application:
    """Создаёт и настраивает Application с зарегистрированными хендлерами.

    Порядок регистрации (важно!):
        1. ConversationHandler — group=0, первым: перехватывает card:* коллбэки
           и ведёт диалог выбора действия над карточкой.
        2. page_nav_cb — сразу за ConvHandler: перехватывает page:* коллбэки.
        3. confirm_move_cb / confirm_cancel_cb — top-level, работают вне диалога
           (обслуживают risky-подтверждение из текстовой команды «перенести»).
        4. CommandHandlers — group=0.
        5. MessageHandler (text_handler) — group=0, последним: роутер текста.

    ConversationHandler не мешает text_handler когда пользователь НЕ в диалоге:
        entry_points реагируют только на callback card:*, обычный текст
        проваливается к text_handler.
    Когда пользователь В диалоге и пишет команду («утро», «вечер» и др.):
        состояния диалога исключают эти слова через ~_MAIN_COMMANDS_FILTER;
        fallbacks перехватывают их, передают в основные хендлеры, сбрасывают диалог.
    """
    from telegram.ext import ApplicationBuilder
    import os
    from dotenv import load_dotenv
    load_dotenv()

    token = os.getenv("TELEGRAM_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_TOKEN не задан в .env")

    app = ApplicationBuilder().token(token).build()

    # ══════════════════════════════════════════════════════════════════════════
    # ConversationHandler — замыкания над cfg
    # ══════════════════════════════════════════════════════════════════════════

    async def card_selected_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Entry: пользователь нажал кнопку карточки → показываем меню действий."""
        query = update.callback_query
        assert query is not None
        await query.answer()

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("card_selected_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        card_id = int(query.data.split(":")[1])
        context.user_data["selected_card_id"] = card_id

        # Получаем актуальный заголовок карточки
        card = None
        try:
            card = await user_ctx.kaiten.get_card(card_id)
            title = card.title if card else f"Карточка #{card_id}"
        except Exception:
            title = f"Карточка #{card_id}"

        context.user_data["selected_card_title"] = title

        # Собираем детали карточки (только заполненные поля)
        detail_lines: list[str] = []
        if card:
            if card.event_time:
                detail_lines.append(
                    f"📅 Событие: {card.event_time.strftime('%d.%m.%Y %H:%M')}"
                )
            if card.due_date_parsed:
                detail_lines.append(
                    f"⏰ Дедлайн: {card.due_date_parsed.strftime('%d.%m.%Y')}"
                )
            if card.importance and card.importance != "среднее":
                detail_lines.append(f"🔥 Важность: {card.importance}")
            if card.size is not None:
                size_str = "от 1 часа" if card.size == 999 else f"{card.size} ч"
                detail_lines.append(f"📏 Размер: {size_str}")
        details_block = ("\n" + "\n".join(detail_lines)) if detail_lines else ""

        await query.edit_message_text(
            f"*{title}*{details_block}\n\nЧто делаем?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_action_keyboard(),
        )
        logger.info("card_selected_cb: card_id={} title={!r}", card_id, title)
        return CARD_ACTION

    # ── Действия над карточкой ────────────────────────────────────────────────

    async def action_done_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """✅ Готово → запрашиваем комментарий перед архивацией."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        context.user_data["pending_action"] = "done"
        # Убираем клавиатуру у старого сообщения
        await query.edit_message_text("✅ Готово", reply_markup=None)
        # Новое сообщение — пользователь получит push-уведомление
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "💬 Добавь комментарий к выполненной задаче\n"
                "_(или напиши «.» чтобы обойтись без комментария)_"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAITING_COMMENT

    async def action_today_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """⏭ Продолжить в другой день → для коротких задач сразу переносим, иначе спрашиваем часы."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        card_id = context.user_data.get("selected_card_id")
        card = await user_ctx.kaiten.get_card(card_id) if (user_ctx and card_id) else None

        # FIX 1: size=None трактуется как 15 мин (DEFAULT_HOURS=0.25) → тоже короткая задача
        if card and card.size != 999 and (card.size is None or card.size <= 0.25):
            # Мягкое сопротивление: критическая задача с дедлайном сегодня/завтра —
            # тот же risky-чек, что в received_hours_cb, иначе короткие задачи без
            # размера проскакивали перенос без подтверждения (баг).
            today_t = datetime.now(TZ_MSK).date()
            tomorrow_t = today_t + timedelta(days=1)
            due_dt_t = card.due_date_parsed
            due_date_t = due_dt_t.date() if due_dt_t else None
            risky = card.importance == "критическое" and due_date_t in (today_t, tomorrow_t)

            if risky:
                title_t = context.user_data.get("selected_card_title", f"#{card_id}")
                context.user_data["pending_move"] = {
                    "kind": "postpone",
                    "card_id": card_id,
                    "hours": None,
                }
                await query.edit_message_text(
                    f"⚠️ «{title_t}» — критическая задача с дедлайном {due_date_t}.\n"
                    f"Точно перенести?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Да, перенести", callback_data="confirm:move"),
                        InlineKeyboardButton("Отмена", callback_data="confirm:cancel"),
                    ]]),
                )
                return ConversationHandler.END

            ok, msg = await _postpone_card(user_ctx, card, hours=None)
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
            if update.effective_chat:
                await _resend_card_buttons(user_ctx, context, update.effective_chat.id)
            return ConversationHandler.END

        # Убираем клавиатуру у старого сообщения
        await query.edit_message_text("⏭ Продолжить в другой день", reply_markup=None)
        # Новое сообщение — пользователь получит push-уведомление
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏱ Сколько часов реально нужно ещё? Перенесу на следующий подходящий день. _(введи целое число)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAITING_HOURS

    async def action_comment_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """💬 Комментарий → запрашиваем текст."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        context.user_data["pending_action"] = "comment"
        # Убираем клавиатуру у старого сообщения
        await query.edit_message_text("💬 Комментарий", reply_markup=None)
        # Новое сообщение — пользователь получит push-уведомление
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="💬 Введи текст комментария:",
        )
        return AWAITING_COMMENT

    async def action_move_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """📅 Перенести → запрашиваем куда."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        # Убираем клавиатуру у старого сообщения
        await query.edit_message_text("📅 Перенести", reply_markup=None)
        # Новое сообщение — пользователь получит push-уведомление
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "📅 Куда перенести?\n"
                "_Например: «завтра», «пятница вечер», «следующая неделя»_"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAITING_MOVE_TARGET

    async def action_back_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """← Назад → возвращаемся к списку карточек."""
        query = update.callback_query
        assert query is not None
        await query.answer()

        chat_id = update.effective_chat.id if update.effective_chat else (
            query.message.chat_id if query.message else None
        )

        try:
            await query.delete_message()
        except Exception:
            pass

        if chat_id:
            user_ctx = cfg.users.get(chat_id)
            if user_ctx is None:
                logger.warning("action_back_cb: unauthorized chat_id={}", chat_id)
            else:
                await _resend_card_buttons(user_ctx, context, chat_id)

        return ConversationHandler.END

    # ── Получение текста от пользователя ─────────────────────────────────────

    async def received_comment_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Получен текст комментария: архивируем или добавляем комментарий.

        Для «готово»: регулярные задачи переносятся на следующий подходящий день,
        обычные — архивируются.
        """
        assert update.message is not None

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("received_comment_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        text      = update.message.text.strip()
        card_id   = context.user_data.get("selected_card_id")
        title     = context.user_data.get("selected_card_title", f"#{card_id}")
        action    = context.user_data.get("pending_action", "comment")

        # «.» = пропустить комментарий (только для done)
        comment_text: str | None = None if text == "." else text

        if action == "done":
            try:
                card = await user_ctx.kaiten.get_card(card_id)
                if card and user_ctx.logic.is_regular_task(card):
                    start_col_id = _next_col_for_regular(
                        card, datetime.now(TZ_MSK).date(), user_ctx.logic.column_ids
                    )
                    slot = await user_ctx.logic.find_slot_for_card(card, start_col_id)
                    if slot is None:
                        slot = (user_ctx.logic.column_ids["Следующая неделя"], "Утро")
                    target_col_id, target_section = slot
                    sort_order = await user_ctx.logic.get_section_sort_order(
                        target_col_id, target_section
                    )
                    if comment_text:
                        await user_ctx.kaiten.add_comment(card_id, comment_text)
                    moved = await user_ctx.kaiten.move_card(card_id, target_col_id, sort_order)
                    if moved:
                        target_col_name = user_ctx.logic.column_name_by_id.get(
                            target_col_id, str(target_col_id)
                        )
                        # B.5: логируем выполнение регулярной задачи
                        await _log_event(
                            user_ctx, "done", title,
                            detail=f"регулярная задача → {target_col_name} / {target_section}",
                        )
                        await update.message.reply_text(
                            f"✅ «{title}» выполнено (регулярная задача) → следующий раз: "
                            f"*{target_col_name} / {target_section}*",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    else:
                        await update.message.reply_text(
                            f"⚠️ Не удалось перенести регулярную «{title}»."
                        )
                else:
                    ok = await user_ctx.logic.archive_card(card_id, comment=comment_text or None)
                    if ok:
                        # B.5: логируем выполнение обычной задачи через архивацию
                        await _log_event(user_ctx, "done", title)
                        await update.message.reply_text(
                            f"✅ «{title}» выполнено и перемещено в архив."
                        )
                    else:
                        await update.message.reply_text(
                            f"⚠️ Не удалось архивировать «{title}»."
                        )
            except Exception as exc:
                logger.exception("received_comment_cb: done error — {}", exc)
                await update.message.reply_text("⚠️ Ошибка при завершении задачи.")

        elif action == "comment":
            if comment_text:
                try:
                    ok = await user_ctx.kaiten.add_comment(card_id, comment_text)
                    if ok:
                        await update.message.reply_text(
                            f"📝 Комментарий добавлен к «{title}»."
                        )
                    else:
                        await update.message.reply_text(
                            f"⚠️ Не удалось добавить комментарий к «{title}»."
                        )
                except Exception as exc:
                    logger.exception("received_comment_cb: add_comment error — {}", exc)
                    await update.message.reply_text("⚠️ Ошибка при добавлении комментария.")
            else:
                await update.message.reply_text("↩️ Комментарий отменён.")

        if update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)

        return ConversationHandler.END

    async def received_hours_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Получено число часов: обновляем size и переносим на следующий слот.

        Если карточка критическая с дедлайном сегодня/завтра — запрашиваем подтверждение.
        """
        assert update.message is not None

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("received_hours_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        text    = update.message.text.strip()
        card_id = context.user_data.get("selected_card_id")

        # Парсим число
        try:
            hours = int(text)
            if hours <= 0:
                raise ValueError("non-positive hours")
        except ValueError:
            await update.message.reply_text(
                "❓ Нужно целое положительное число (например: `2`).\nПопробуй ещё раз:",
                parse_mode=ParseMode.MARKDOWN,
            )
            return AWAITING_HOURS  # остаёмся в том же состоянии

        # Получаем карточку, проверяем risky-критерий, затем переносим
        try:
            card_obj = await user_ctx.kaiten.get_card(card_id)
            if card_obj is None:
                await update.message.reply_text("⚠️ Карточка не найдена.")
                return ConversationHandler.END

            # Мягкое сопротивление: критическая задача с дедлайном сегодня/завтра
            today_h = datetime.now(TZ_MSK).date()
            tomorrow_h = today_h + timedelta(days=1)
            due_dt_h = card_obj.due_date_parsed
            due_date_h = due_dt_h.date() if due_dt_h else None
            risky = card_obj.importance == "критическое" and due_date_h in (today_h, tomorrow_h)

            if risky:
                title_h = context.user_data.get("selected_card_title", f"#{card_id}")
                context.user_data["pending_move"] = {
                    "kind": "postpone",
                    "card_id": card_id,
                    "hours": hours,
                }
                await update.message.reply_text(
                    f"⚠️ «{title_h}» — критическая задача с дедлайном {due_date_h}.\n"
                    f"Точно перенести (часы: {hours})?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Да, перенести", callback_data="confirm:move"),
                        InlineKeyboardButton("Отмена", callback_data="confirm:cancel"),
                    ]]),
                )
                # Диалог завершается; confirm_move_cb/confirm_cancel_cb — top-level хендлеры
                return ConversationHandler.END

            ok, msg = await _postpone_card(user_ctx, card_obj, hours)
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception as exc:
            logger.exception("received_hours_cb: error — {}", exc)
            await update.message.reply_text("⚠️ Ошибка при переносе карточки.")

        if update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)

        return ConversationHandler.END

    async def received_move_target_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Получен текст с целью переноса: парсим через Claude и двигаем карточку.

        Если карточка критическая с дедлайном сегодня/завтра — запрашиваем подтверждение.
        """
        assert update.message is not None

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("received_move_target_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        text    = update.message.text.strip()
        card_id = context.user_data.get("selected_card_id")
        title   = context.user_data.get("selected_card_title", f"#{card_id}")

        try:
            intent = await cfg.claude.parse_intent(f"перенести {text}")
        except Exception as exc:
            logger.exception("received_move_target_cb: parse_intent error — {}", exc)
            await update.message.reply_text(
                "⚠️ Не удалось разобрать куда перенести.\nПопробуй ещё раз:"
            )
            return AWAITING_MOVE_TARGET

        column_name = intent.get("column")
        deadline    = intent.get("deadline")
        section     = intent.get("section") or "Утро"

        target_col_id: int | None = None
        target_col_name: str | None = None

        if deadline:
            try:
                target_date = date.fromisoformat(deadline)
                target_col_id = user_ctx.logic.resolve_column_for_date(target_date)
                target_col_name = user_ctx.logic.column_name_by_id.get(
                    target_col_id, str(target_col_id)
                )
            except ValueError:
                logger.warning(
                    "received_move_target_cb: не удалось разобрать deadline={!r}", deadline
                )

        if target_col_id is None:
            if column_name and column_name in user_ctx.logic.column_ids:
                target_col_id   = user_ctx.logic.column_ids[column_name]
                target_col_name = column_name
            else:
                tomorrow        = datetime.now(TZ_MSK).date() + timedelta(days=1)
                target_col_name = WEEKDAY_COLUMNS[tomorrow.weekday()]
                target_col_id   = user_ctx.logic.column_ids[target_col_name]

        # Мягкое сопротивление: критическая задача с дедлайном сегодня/завтра
        try:
            card = await user_ctx.kaiten.get_card(card_id)
        except Exception:
            card = None
        today = datetime.now(TZ_MSK).date()
        tomorrow = today + timedelta(days=1)
        due_dt = card.due_date_parsed if card else None
        due_date = due_dt.date() if due_dt else None
        risky = bool(card) and card.importance == "критическое" and due_date in (today, tomorrow)

        # Перенос в «На контроле» не требует risky-подтверждения — выполняется сразу,
        # но после переноса запускается диалог о переносе дедлайна (ниже).
        if risky and section != "На контроле":
            context.user_data["pending_move"] = {
                "kind": "move",
                "card_id": card_id,
                "title": title,
                "target_col_id": target_col_id,
                "target_col_name": target_col_name,
                "section": section,
            }
            await update.message.reply_text(
                f"⚠️ «{title}» — критическая задача с дедлайном {due_date.isoformat()}.\n"
                f"Точно перенести на {target_col_name}?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Да, перенести", callback_data="confirm:move"),
                    InlineKeyboardButton("Отмена", callback_data="confirm:cancel"),
                ]]),
            )
            # Диалог завершается; confirm_move_cb/confirm_cancel_cb — top-level хендлеры
            return ConversationHandler.END

        try:
            sort_order = await user_ctx.logic.get_section_sort_order(target_col_id, section)
            moved = await user_ctx.kaiten.move_card(card_id, target_col_id, sort_order)
            if moved:
                # B.6: логируем перенос через кнопку «Перенести» (без risky-подтверждения)
                await _log_event(user_ctx, "moved", title, detail=f"{target_col_name} / {section}")
                await update.message.reply_text(
                    f"📦 «{title}» → *{target_col_name} / {section}*",
                    parse_mode=ParseMode.MARKDOWN,
                )
                if section == "На контроле":
                    try:
                        today_str = datetime.now(TZ_MSK).date().isoformat()
                        await user_ctx.kaiten.add_comment(
                            card_id, f"Задача переведена на контроль {today_str}"
                        )
                    except Exception as exc:
                        logger.warning(
                            "received_move_target_cb: не удалось добавить комментарий о контроле — {}",
                            exc,
                        )
                    # Для критической задачи с близким дедлайном — запускаем диалог
                    # о переносе дедлайна вместо пропущенного risky-подтверждения.
                    if risky:
                        context.user_data["pending_deadline_reschedule"] = {
                            "card_id": card_id,
                            "title": title,
                        }
                        await update.message.reply_text(
                            "На какой день перенести дедлайн этой задачи?\n"
                            "_«.» — оставить без изменений, «отменить» — убрать дедлайн, или напиши дату_",
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        # _resend_card_buttons будет вызван после ответа пользователя
                        return ConversationHandler.END
            else:
                await update.message.reply_text(f"⚠️ Не удалось переместить «{title}».")
        except Exception as exc:
            logger.exception("received_move_target_cb: move error — {}", exc)
            await update.message.reply_text("⚠️ Ошибка при перемещении карточки.")

        if update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)

        return ConversationHandler.END

    # ── Подтверждение переноса критической задачи ─────────────────────────────
    # Зарегистрированы как top-level хендлеры (не в states ConversationHandler),
    # чтобы работать и из текстовой команды «перенести», и после кнопки «Перенести».

    async def confirm_move_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Пользователь подтвердил перенос критической задачи — выполняем."""
        query = update.callback_query
        assert query is not None
        await query.answer()

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        pending = context.user_data.get("pending_move")

        if not pending or user_ctx is None:
            await query.edit_message_text("⚠️ Действие устарело, попробуй заново.")
            return ConversationHandler.END

        if pending["kind"] == "move":
            try:
                sort_order = await user_ctx.logic.get_section_sort_order(
                    pending["target_col_id"], pending["section"]
                )
                moved = await user_ctx.kaiten.move_card(
                    pending["card_id"], pending["target_col_id"], sort_order
                )
                if moved:
                    # B.7: логируем перенос критической задачи после подтверждения (kind=="move")
                    await _log_event(
                        user_ctx, "moved", pending["title"],
                        detail=f"{pending['target_col_name']} / {pending['section']}",
                    )
                    await query.edit_message_text(
                        f"📦 «{pending['title']}» → *{pending['target_col_name']} / {pending['section']}*",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    if pending["section"] == "На контроле":
                        try:
                            today_str = datetime.now(TZ_MSK).date().isoformat()
                            await user_ctx.kaiten.add_comment(
                                pending["card_id"],
                                f"Задача переведена на контроль {today_str}",
                            )
                        except Exception as exc:
                            logger.warning("confirm_move_cb: комментарий о контроле — {}", exc)
                else:
                    await query.edit_message_text(
                        f"⚠️ Не удалось переместить «{pending['title']}»."
                    )
            except Exception as exc:
                logger.exception("confirm_move_cb: move error — {}", exc)
                await query.edit_message_text("⚠️ Ошибка при перемещении карточки.")

        elif pending["kind"] == "postpone":
            # Ветку postpone НЕ трогаем — _postpone_card уже логирует "moved" через B.3
            try:
                card = await user_ctx.kaiten.get_card(pending["card_id"])
                if card is None:
                    await query.edit_message_text("⚠️ Карточка не найдена.")
                else:
                    ok, msg = await _postpone_card(user_ctx, card, pending["hours"])
                    await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
            except Exception as exc:
                logger.exception("confirm_move_cb: postpone error — {}", exc)
                await query.edit_message_text("⚠️ Ошибка при переносе карточки.")

        context.user_data.pop("pending_move", None)
        if update.effective_chat and user_ctx:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)
        return ConversationHandler.END

    async def confirm_cancel_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Пользователь отменил перенос критической задачи."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        await query.edit_message_text("↩️ Перенос отменён.")
        context.user_data.pop("pending_move", None)
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx and update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)
        return ConversationHandler.END

    # ── Кнопка «Совет» ───────────────────────────────────────────────────────

    async def action_advice_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """🤖 Совет → запрашиваем вопрос пользователя."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        # Убираем клавиатуру у старого сообщения
        await query.edit_message_text("🤖 Совет", reply_markup=None)
        # Новое сообщение — пользователь получит push-уведомление
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "🤖 Какой вопрос по этой задаче?\n"
                "_Например: «с чего начать», «какие риски», «что учесть»_"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAITING_QUESTION

    async def received_question_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Получен вопрос: запрашиваем совет у Claude, добавляем комментарий к карточке."""
        assert update.message is not None

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("received_question_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        question = update.message.text.strip()
        card_id  = context.user_data.get("selected_card_id")
        title    = context.user_data.get("selected_card_title", f"#{card_id}")

        await update.message.reply_text("🤖 Думаю над советом…")

        # Получаем полный контекст карточки
        description: str | None = None
        comments: list[str] = []
        try:
            card = await user_ctx.kaiten.get_card(card_id)
            if card:
                description = card.description
            comments = await user_ctx.kaiten.get_comments(card_id)
        except Exception as exc:
            logger.warning("received_question_cb: не удалось загрузить контекст карточки — {}", exc)

        # Запрашиваем совет у Claude
        try:
            answer = await cfg.claude.generate_card_advice(
                question=question,
                card_title=title,
                description=description,
                comments=comments,
            )
        except Exception as exc:
            logger.exception("received_question_cb: generate_card_advice error — {}", exc)
            await update.message.reply_text("⚠️ Не удалось получить совет. Попробуй позже.")
            if update.effective_chat:
                await _resend_card_buttons(user_ctx, context, update.effective_chat.id)
            return ConversationHandler.END

        # Отправляем ответ в чат
        await _reply(update, answer)

        # Сохраняем вопрос и ответ как комментарий к карточке
        note = f"❓ Вопрос: {question}\n💡 Ответ: {answer}"
        try:
            await user_ctx.kaiten.add_comment(card_id, note)
            logger.info("received_question_cb: совет сохранён в карточку id={}", card_id)
        except Exception as exc:
            logger.warning("received_question_cb: не удалось сохранить совет в карточку — {}", exc)

        if update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)

        return ConversationHandler.END

    # ── Кнопка «Напоминалка» ─────────────────────────────────────────────────

    async def action_reminder_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """🔔 Напоминалка → запрашиваем дату и время."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        # Убираем клавиатуру у старого сообщения
        await query.edit_message_text("🔔 Напоминалка", reply_markup=None)
        # Новое сообщение — пользователь получит push-уведомление
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "🔔 Когда напомнить?\n"
                "_Например: «завтра в 14:00», «пятница 09:30», «20 июня 10:00»_"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return AWAITING_REMINDER_TIME

    async def received_reminder_time_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Получены дата/время: устанавливаем event_time и добавляем тег «напомнить»."""
        assert update.message is not None

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("received_reminder_time_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        text    = update.message.text.strip()
        card_id = context.user_data.get("selected_card_id")
        title   = context.user_data.get("selected_card_title", f"#{card_id}")

        # Парсим дату через Claude (префикс «создать задачу» чтобы Claude понял контекст)
        try:
            intent = await cfg.claude.parse_intent(f"создать задачу {text}")
        except Exception as exc:
            logger.exception("received_reminder_time_cb: parse_intent error — {}", exc)
            await update.message.reply_text("⚠️ Не удалось разобрать дату. Попробуй ещё раз:")
            return AWAITING_REMINDER_TIME

        event_date: str | None = intent.get("deadline")  # "YYYY-MM-DD"

        if not event_date:
            await update.message.reply_text(
                "❓ Не удалось определить дату.\n"
                "Попробуй написать точнее, например: «завтра в 14:00» или «пятница 09:30»"
            )
            return AWAITING_REMINDER_TIME

        # Извлекаем время из сырого текста пользователя (HH:MM или H:MM)
        time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
        if time_match:
            hh = int(time_match.group(1))
            mm = int(time_match.group(2))
            time_str = f"{hh:02d}:{mm:02d}:00"
        else:
            # Нет явного времени — берём дефолт по секции
            section_defaults = {"Утро": "09:00:00", "День": "14:00:00", "Вечер": "19:00:00"}
            section  = intent.get("section") or "Утро"
            time_str = section_defaults.get(section, "09:00:00")

        event_time_obj = {"date": event_date, "time": time_str, "tzOffset": 180}
        logger.info(
            "received_reminder_time_cb: card_id={} event_time={}", card_id, event_time_obj
        )

        # Обновляем event_time карточки
        try:
            await user_ctx.kaiten.update_card(
                card_id,
                properties={"id_590358": event_time_obj},
            )
        except Exception as exc:
            logger.exception("received_reminder_time_cb: update event_time error — {}", exc)
            await update.message.reply_text("⚠️ Не удалось установить время события.")
            if update.effective_chat:
                await _resend_card_buttons(user_ctx, context, update.effective_chat.id)
            return ConversationHandler.END

        # Добавляем тег «напомнить» если его ещё нет
        try:
            card = await user_ctx.kaiten.get_card(card_id)
            current_tag_names = [t.name for t in card.tags] if card else []
            if "напомнить" not in current_tag_names:
                await user_ctx.kaiten.add_tag_by_name(card_id, "напомнить")
                logger.info("received_reminder_time_cb: тег «напомнить» добавлен к id={}", card_id)
        except Exception as exc:
            logger.warning("received_reminder_time_cb: не удалось добавить тег — {}", exc)

        await update.message.reply_text(
            f"🔔 Напоминание установлено для «{title}»:\n"
            f"📅 {event_date} в {time_str[:5]}",
        )

        if update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)

        return ConversationHandler.END

    # ── Кнопка «Редактировать» ───────────────────────────────────────────────

    async def action_edit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """✏️ Редактировать → открываем конструктор, предзаполненный текущими значениями."""
        query = update.callback_query
        assert query is not None
        await query.answer()

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            return ConversationHandler.END

        card_id = context.user_data.get("selected_card_id")
        try:
            card = await user_ctx.kaiten.get_card(card_id)
        except Exception as exc:
            logger.exception("action_edit_cb: get_card error — {}", exc)
            card = None
        if card is None:
            await query.edit_message_text("⚠️ Карточка не найдена.")
            return ConversationHandler.END

        tag_names = {t.name for t in card.tags} if card.tags else set()
        regularity = None
        for reg_name in ("по будням", "по выходным", "ежедневно", "еженедельно"):
            if reg_name in tag_names:
                regularity = reg_name
                break

        et  = card.event_time
        due = card.due_date_parsed

        data = {
            "title": card.title,
            "size": card.size,
            "event_date": et.date().isoformat() if et else None,
            "event_time": et.strftime("%H:%M:%S") if et else None,
            "deadline": due.date().isoformat() if due else None,
            "regularity": regularity,
            "weekday": card.weekday,
            "importance": card.importance,
            "reminder": "напомнить" in tag_names,
            "description": card.description,
            "_edit_card_id": card_id,
        }
        data["_original"] = dict(data)
        context.user_data["new_task"] = data

        text, markup = _render_task_constructor(data)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return CREATE_MENU

    async def action_description_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """📄 Описание и комментарии → отправляем описание + список комментариев новым сообщением.

        Меню действий карточки остаётся видимым — диалог не завершается.
        Предыдущее сообщение с описанием (last_description_msg_id) удаляется перед отправкой нового.
        """
        query = update.callback_query
        assert query is not None
        await query.answer()

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            return CARD_ACTION

        card_id = context.user_data.get("selected_card_id")

        # Удаляем предыдущее сообщение с описанием если было
        old_desc_msg_id = context.user_data.pop("last_description_msg_id", None)
        if old_desc_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=old_desc_msg_id)
            except Exception:
                pass

        try:
            card = await user_ctx.kaiten.get_card(card_id)
            comments = await user_ctx.kaiten.get_comments(card_id)
        except Exception as exc:
            logger.warning("action_description_cb: ошибка загрузки — {}", exc)
            try:
                await query.answer("⚠️ Не удалось загрузить данные карточки", show_alert=True)
            except Exception:
                pass
            return CARD_ACTION

        description = (card.description or "").strip() if card else ""
        desc_text = description if description else "Без описания"

        text_lines = [f"📄 *Описание:*\n{desc_text}"]
        if comments:
            text_lines.append(f"\n💬 *Комментарии ({len(comments)}):*")
            for i, comment in enumerate(comments, 1):
                text_lines.append(f"{i}. {comment}")
        else:
            text_lines.append("\n💬 Комментариев нет")

        full_text = "\n".join(text_lines)
        parts = _split_text(full_text)

        last_sent_id: int | None = None
        for part in parts:
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id, text=part, parse_mode=ParseMode.MARKDOWN
                )
                last_sent_id = msg.message_id
            except Exception:
                try:
                    msg = await context.bot.send_message(chat_id=chat_id, text=part)
                    last_sent_id = msg.message_id
                except Exception as exc2:
                    logger.error("action_description_cb: ошибка отправки части — {}", exc2)

        if last_sent_id is not None:
            context.user_data["last_description_msg_id"] = last_sent_id

        logger.info("action_description_cb: card_id={} комментариев={}", card_id, len(comments))
        return CARD_ACTION

    async def received_edit_title_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Получено новое название карточки в режиме редактирования."""
        assert update.message is not None
        data = context.user_data.get("new_task")
        if data is None:
            return ConversationHandler.END
        title = update.message.text.strip()
        if not title:
            await update.message.reply_text("❓ Название не может быть пустым. Попробуй ещё раз:")
            return CREATE_AWAITING_EDIT_TITLE
        data["title"] = title
        text, markup = _render_task_constructor(data)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return CREATE_MENU

    # ── Fallbacks диалога: известные команды прерывают диалог и выполняются ───

    async def conv_fallback_text_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Пользователь написал команду будучи в диалоге — выходим и выполняем."""
        assert update.message is not None

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("conv_fallback_text_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        text  = update.message.text.strip()
        lower = text.lower()

        logger.info("conv_fallback: выход из диалога, text={!r}", text[:60])

        if lower == "утро":
            await _handle_morning(update, user_ctx, context)
        elif lower == "вечер":
            await _handle_evening(update, user_ctx)
        elif lower in ("пересобрать", "перепланируй"):
            await _handle_replan(update, user_ctx, context)
        elif re.match(r"^(создать|создай)\b", lower):
            await _handle_create(update, cfg, _strip_command_prefix(text, "создать", "создай"), user_ctx)
        elif re.match(r"^(готово|выполнено|сделал|сделано)\b", lower):
            await _handle_done(
                update, cfg,
                _strip_command_prefix(text, "готово", "выполнено", "сделал", "сделано"),
                user_ctx,
            )
        elif re.match(r"^(перенести|перенеси|переместить)\b", lower):
            await _handle_move(
                update, cfg,
                _strip_command_prefix(text, "перенести", "перенеси", "переместить"),
                user_ctx,
                context,
            )
        elif re.match(r"^(заметка|комментарий|добавь заметку)\b", lower):
            await _handle_note(
                update, cfg,
                _strip_command_prefix(text, "заметка", "комментарий", "добавь заметку"),
                user_ctx,
            )
        else:
            # Неизвестный текст — показываем подсказку и сбрасываем диалог
            await _reply(update, HELP_TEXT)

        return ConversationHandler.END

    async def conv_cancel_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """/cancel — явная отмена текущего диалога."""
        if update.message:
            await update.message.reply_text("↩️ Действие отменено.")
            chat_id = update.effective_chat.id if update.effective_chat else None
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("↩️ Действие отменено.")
            chat_id = update.effective_chat.id if update.effective_chat else None
        else:
            chat_id = None
        if chat_id is not None:
            user_ctx = cfg.users.get(chat_id)
            if user_ctx is not None:
                await _resend_card_buttons(user_ctx, context, chat_id)
        return ConversationHandler.END

    # ── Навигация по страницам карточек ──────────────────────────────────────

    async def page_nav_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Навигация по страницам списка карточек (page:N callback)."""
        query = update.callback_query
        await query.answer()
        chat_id = update.effective_chat.id
        user_ctx = cfg.users.get(chat_id)
        if user_ctx is None:
            return
        page = int(query.data.split(":")[1])
        context.user_data["view_ctx"] = {"scope": "today", "page": page}
        today_col_id = user_ctx.logic.get_today_column_id()
        today_cards = await user_ctx.kaiten.get_cards(today_col_id)
        try:
            await query.delete_message()
        except Exception:
            pass
        await send_card_buttons(today_cards, context.bot, chat_id, page=page, context=context)

    # ── Просмотр других колонок ──────────────────────────────────────────────

    async def cmd_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/other — меню выбора области просмотра других колонок."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            return
        kb = [
            [InlineKeyboardButton("Сегодня",              callback_data="oc:today:0")],
            [InlineKeyboardButton("Другие дни недели",    callback_data="oc:otherdays:0")],
            [InlineKeyboardButton("Следующая неделя",     callback_data="oc:nextweek:0")],
            [InlineKeyboardButton("Далёкие времена",      callback_data="oc:faraway:0")],
        ]
        await update.message.reply_text(
            "Какие задачи посмотреть?", reply_markup=InlineKeyboardMarkup(kb)
        )

    async def oc_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Навигация по областям просмотра других колонок (callback_data 'oc:<scope>:<page>')."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            return
        _, scope, page_str = query.data.split(":")
        try:
            page = int(page_str)
        except ValueError:
            page = 0
        context.user_data["view_ctx"] = {"scope": scope, "page": page}
        try:
            await query.delete_message()
        except Exception:
            pass
        await _render_other_columns_page(user_ctx, scope, page, context.bot, chat_id, context=context)

    # ── Конструктор создания задачи ──────────────────────────────────────────

    async def newtask_entry_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Точка входа в конструктор: /newtask или голое «создать»/«создай»."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("newtask_entry_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        context.user_data["new_task"] = {
            "title": None, "size": None,
            "event_date": None, "event_time": None,
            "deadline": None, "regularity": None, "weekday": None,
            "importance": None, "reminder": False,
        }
        if update.message:
            await update.message.reply_text("✏️ Название задачи?")
        return CREATE_AWAITING_TITLE

    async def received_title_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Получено название новой задачи — показываем конструктор."""
        assert update.message is not None
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            return ConversationHandler.END

        title = update.message.text.strip()
        if not title:
            await update.message.reply_text("❓ Название не может быть пустым. Попробуй ещё раз:")
            return CREATE_AWAITING_TITLE

        context.user_data["new_task"]["title"] = title
        text, markup = _render_task_constructor(context.user_data["new_task"])
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return CREATE_MENU

    async def nt_router_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Роутер кнопок конструктора (namespace callback_data 'nt:').

        ЗАГЛУШКА — полная логика подменю (размер/событие/дедлайн/регулярность/важность/
        напоминалка/готово) будет добавлена следующей задачей через Edit в это же тело
        функции. Пока обрабатывает только «отмена» и перерисовку по умолчанию — так весь
        конструктор уже импортируется и синтаксически корректен, «Отмена» и вход в диалог
        уже полностью рабочие для тестирования.
        """
        query = update.callback_query
        assert query is not None
        await query.answer()

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            return ConversationHandler.END

        data = context.user_data.get("new_task")
        if data is None:
            await query.edit_message_text("⚠️ Сессия устарела, начни заново командой /newtask.")
            return ConversationHandler.END

        parts  = query.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        sub    = parts[2] if len(parts) > 2 else None

        if action == "cancel":
            await query.edit_message_text("❌ Отмена")
            context.user_data.pop("new_task", None)
            if chat_id is not None and user_ctx is not None:
                await _resend_card_buttons(user_ctx, context, chat_id)
            return ConversationHandler.END

        async def _redraw() -> None:
            text_r, markup_r = _render_task_constructor(data)
            await query.edit_message_text(text_r, parse_mode=ParseMode.MARKDOWN, reply_markup=markup_r)

        if action == "size" and sub is None:
            kb = [
                [InlineKeyboardButton("15 мин", callback_data="nt:size:15m"),
                 InlineKeyboardButton("30 мин", callback_data="nt:size:30m")],
                [InlineKeyboardButton("1 ч", callback_data="nt:size:1h"),
                 InlineKeyboardButton("2 ч", callback_data="nt:size:2h")],
                [InlineKeyboardButton("3 ч", callback_data="nt:size:3h"),
                 InlineKeyboardButton("От 1ч, сколько влезет", callback_data="nt:size:flex")],
                [InlineKeyboardButton("Своё значение", callback_data="nt:size:custom")],
                [InlineKeyboardButton("← Назад", callback_data="nt:back")],
            ]
            await query.edit_message_text("Выбери размер:", reply_markup=InlineKeyboardMarkup(kb))
            return CREATE_MENU
        if action == "size" and sub is not None:
            if sub == "custom":
                await query.edit_message_text("Своё значение в часах (например 1.5):")
                return CREATE_AWAITING_SIZE_TEXT
            mapping = {"15m": None, "30m": 0.5, "1h": 1, "2h": 2, "3h": 3, "flex": 999}
            if sub in mapping:
                data["size"] = mapping[sub]
            await _redraw()
            return CREATE_MENU

        if action == "event" and sub is None:
            kb = [
                [InlineKeyboardButton("Сегодня", callback_data="nt:event:today"),
                 InlineKeyboardButton("Завтра", callback_data="nt:event:tomorrow")],
                [InlineKeyboardButton("Своя дата", callback_data="nt:event:custom")],
                [InlineKeyboardButton("Без события", callback_data="nt:event:clear")],
                [InlineKeyboardButton("← Назад", callback_data="nt:back")],
            ]
            await query.edit_message_text("Когда событие?", reply_markup=InlineKeyboardMarkup(kb))
            return CREATE_MENU
        if action == "event" and sub is not None:
            if sub == "clear":
                data["event_date"] = None
                data["event_time"] = None
                data["reminder"] = False
                await _redraw()
                return CREATE_MENU
            if sub == "today":
                data["_event_pending_date"] = datetime.now(TZ_MSK).date().isoformat()
                await query.edit_message_text("Во сколько? Например: 15:00")
                return CREATE_AWAITING_EVENT_TEXT
            if sub == "tomorrow":
                data["_event_pending_date"] = (datetime.now(TZ_MSK).date() + timedelta(days=1)).isoformat()
                await query.edit_message_text("Во сколько? Например: 15:00")
                return CREATE_AWAITING_EVENT_TEXT
            if sub == "custom":
                data.pop("_event_pending_date", None)
                await query.edit_message_text(
                    "Когда событие? Например: «25 июля в 15:00», «пятница в 10:00»"
                )
                return CREATE_AWAITING_EVENT_TEXT

        if action == "deadline" and sub is None:
            kb = [
                [InlineKeyboardButton("Сегодня", callback_data="nt:deadline:today"),
                 InlineKeyboardButton("Завтра", callback_data="nt:deadline:tomorrow")],
                [InlineKeyboardButton("Своя дата", callback_data="nt:deadline:custom")],
                [InlineKeyboardButton("Без дедлайна", callback_data="nt:deadline:clear")],
                [InlineKeyboardButton("← Назад", callback_data="nt:back")],
            ]
            await query.edit_message_text("Когда дедлайн?", reply_markup=InlineKeyboardMarkup(kb))
            return CREATE_MENU
        if action == "deadline" and sub is not None:
            if sub == "clear":
                data["deadline"] = None
                await _redraw()
                return CREATE_MENU
            if sub == "today":
                data["deadline"] = datetime.now(TZ_MSK).date().isoformat()
                await _redraw()
                return CREATE_MENU
            if sub == "tomorrow":
                data["deadline"] = (datetime.now(TZ_MSK).date() + timedelta(days=1)).isoformat()
                await _redraw()
                return CREATE_MENU
            if sub == "custom":
                await query.edit_message_text(
                    "Когда дедлайн? Например: «25 июля», «пятница», «через 3 дня»"
                )
                return CREATE_AWAITING_DEADLINE_TEXT

        if action == "regularity" and sub is None:
            kb = [
                [InlineKeyboardButton("Разовая", callback_data="nt:reg:none")],
                [InlineKeyboardButton("По будням", callback_data="nt:reg:weekdays"),
                 InlineKeyboardButton("По выходным", callback_data="nt:reg:weekends")],
                [InlineKeyboardButton("Ежедневно", callback_data="nt:reg:daily"),
                 InlineKeyboardButton("Еженедельно", callback_data="nt:reg:weekly")],
                [InlineKeyboardButton("← Назад", callback_data="nt:back")],
            ]
            await query.edit_message_text("Регулярность:", reply_markup=InlineKeyboardMarkup(kb))
            return CREATE_MENU
        if action == "reg" and sub is not None:
            reg_mapping = {
                "none": None, "weekdays": "по будням", "weekends": "по выходным",
                "daily": "ежедневно",
            }
            if sub == "weekly":
                kb = [
                    [InlineKeyboardButton(wd, callback_data=f"nt:wd:{wd}")]
                    for wd in ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
                ]
                kb.append([InlineKeyboardButton("← Назад", callback_data="nt:back")])
                await query.edit_message_text("Какой день недели?", reply_markup=InlineKeyboardMarkup(kb))
                return CREATE_MENU
            if sub in reg_mapping:
                data["regularity"] = reg_mapping[sub]
                data["weekday"] = None
                await _redraw()
                return CREATE_MENU
        if action == "wd" and sub is not None:
            data["regularity"] = "еженедельно"
            data["weekday"] = sub
            await _redraw()
            return CREATE_MENU

        if action == "importance" and sub is None:
            kb = [
                [InlineKeyboardButton("Обычная", callback_data="nt:imp:normal")],
                [InlineKeyboardButton("Важная", callback_data="nt:imp:high")],
                [InlineKeyboardButton("Критическая", callback_data="nt:imp:critical")],
                [InlineKeyboardButton("← Назад", callback_data="nt:back")],
            ]
            await query.edit_message_text("Важность:", reply_markup=InlineKeyboardMarkup(kb))
            return CREATE_MENU
        if action == "imp" and sub is not None:
            imp_mapping = {"normal": None, "high": "важное", "critical": "критическое"}
            if sub in imp_mapping:
                data["importance"] = imp_mapping[sub]
            await _redraw()
            return CREATE_MENU

        if action == "reminder":
            if not data.get("event_date"):
                await query.answer("Сначала укажи событие", show_alert=True)
                return CREATE_MENU
            data["reminder"] = not data.get("reminder", False)
            await _redraw()
            return CREATE_MENU

        if action == "description":
            await query.edit_message_text(
                "📄 Введи описание задачи:\n"
                "_(или «.» чтобы оставить без изменений)_",
                parse_mode=ParseMode.MARKDOWN,
            )
            return CREATE_AWAITING_DESCRIPTION

        if action == "back":
            await _redraw()
            return CREATE_MENU

        if action == "title":
            await query.edit_message_text("Новое название:")
            return CREATE_AWAITING_EDIT_TITLE

        if action == "done":
            if data.get("_edit_card_id"):
                return await _finalize_edit_task(query, context, user_ctx, data)
            return await _finalize_new_task(query, context, user_ctx, data)

        text, markup = _render_task_constructor(data)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return CREATE_MENU

    async def received_size_text_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        assert update.message is not None
        data = context.user_data.get("new_task")
        if data is None:
            return ConversationHandler.END
        text = update.message.text.strip().replace(",", ".")
        try:
            hours = float(text)
            if hours <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❓ Нужно положительное число часов (например 1.5). Попробуй ещё раз:"
            )
            return CREATE_AWAITING_SIZE_TEXT
        data["size"] = hours
        text_out, markup = _render_task_constructor(data)
        await update.message.reply_text(text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return CREATE_MENU

    async def received_event_text_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        assert update.message is not None
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        data = context.user_data.get("new_task")
        if user_ctx is None or data is None:
            return ConversationHandler.END

        text = update.message.text.strip()
        pending_date = data.pop("_event_pending_date", None)

        if pending_date:
            m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
            if not m:
                await update.message.reply_text("❓ Не понял время. Например: 15:00. Попробуй ещё раз:")
                data["_event_pending_date"] = pending_date
                return CREATE_AWAITING_EVENT_TEXT
            data["event_date"] = pending_date
            data["event_time"] = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}:00"
        else:
            try:
                intent = await cfg.claude.parse_intent(f"создать задачу {text}")
            except Exception as exc:
                logger.exception("received_event_text_cb: parse_intent — {}", exc)
                await update.message.reply_text("⚠️ Не удалось разобрать дату. Попробуй ещё раз:")
                return CREATE_AWAITING_EVENT_TEXT
            event_date = intent.get("deadline")
            if not event_date:
                await update.message.reply_text("❓ Не удалось определить дату. Попробуй точнее:")
                return CREATE_AWAITING_EVENT_TEXT
            m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
            time_str = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}:00" if m else "09:00:00"
            data["event_date"] = event_date
            data["event_time"] = time_str

        text_out, markup = _render_task_constructor(data)
        await update.message.reply_text(text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return CREATE_MENU

    async def received_deadline_text_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        assert update.message is not None
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        data = context.user_data.get("new_task")
        if user_ctx is None or data is None:
            return ConversationHandler.END

        text = update.message.text.strip()
        try:
            intent = await cfg.claude.parse_intent(f"создать задачу дедлайн {text}")
        except Exception as exc:
            logger.exception("received_deadline_text_cb: parse_intent — {}", exc)
            await update.message.reply_text("⚠️ Не удалось разобрать дату. Попробуй ещё раз:")
            return CREATE_AWAITING_DEADLINE_TEXT
        deadline = intent.get("deadline")
        if not deadline:
            await update.message.reply_text("❓ Не удалось определить дату. Попробуй точнее:")
            return CREATE_AWAITING_DEADLINE_TEXT
        data["deadline"] = deadline

        text_out, markup = _render_task_constructor(data)
        await update.message.reply_text(text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        return CREATE_MENU

    async def received_description_text_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Получен текст описания задачи: сохраняем в data и возвращаем в конструктор.

        «.» (одна точка) трактуется как «без изменений» — описание не меняется.
        """
        assert update.message is not None
        data = context.user_data.get("new_task")
        if data is None:
            return ConversationHandler.END
        text = update.message.text.strip()
        if text != ".":
            data["description"] = text
        text_out, markup = _render_task_constructor(data)
        try:
            await update.message.reply_text(
                text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=markup
            )
        except Exception:
            await update.message.reply_text(text_out, reply_markup=markup)
        return CREATE_MENU

    # ── Сборка ConversationHandler ────────────────────────────────────────────

    # Текстовый фильтр для состояний ожидания: не реагирует на известные команды
    _text_not_cmd = filters.TEXT & ~filters.COMMAND & ~_MAIN_COMMANDS_FILTER

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(card_selected_cb, pattern=r"^card:\d+$"),
            CommandHandler("newtask", newtask_entry_cb),
            MessageHandler(filters.Regex(r"(?i)^(создать|создай)\s*$"), newtask_entry_cb),
        ],
        states={
            CARD_ACTION: [
                CallbackQueryHandler(action_done_cb,        pattern=r"^action:done$"),
                CallbackQueryHandler(action_today_cb,       pattern=r"^action:today$"),
                CallbackQueryHandler(action_comment_cb,     pattern=r"^action:comment$"),
                CallbackQueryHandler(action_move_cb,        pattern=r"^action:move$"),
                CallbackQueryHandler(action_advice_cb,      pattern=r"^action:advice$"),
                CallbackQueryHandler(action_reminder_cb,    pattern=r"^action:reminder$"),
                CallbackQueryHandler(action_edit_cb,        pattern=r"^action:edit$"),
                CallbackQueryHandler(action_description_cb, pattern=r"^action:description$"),
                CallbackQueryHandler(action_back_cb,        pattern=r"^action:back$"),
            ],
            AWAITING_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_comment_cb),
            ],
            AWAITING_HOURS: [
                MessageHandler(_text_not_cmd, received_hours_cb),
            ],
            AWAITING_MOVE_TARGET: [
                MessageHandler(_text_not_cmd, received_move_target_cb),
            ],
            AWAITING_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_question_cb),
            ],
            AWAITING_REMINDER_TIME: [
                MessageHandler(_text_not_cmd, received_reminder_time_cb),
            ],
            CREATE_AWAITING_TITLE: [
                MessageHandler(_text_not_cmd, received_title_cb),
            ],
            CREATE_MENU: [
                CallbackQueryHandler(nt_router_cb, pattern=r"^nt:"),
            ],
            CREATE_AWAITING_SIZE_TEXT: [
                MessageHandler(_text_not_cmd, received_size_text_cb),
            ],
            CREATE_AWAITING_EVENT_TEXT: [
                MessageHandler(_text_not_cmd, received_event_text_cb),
            ],
            CREATE_AWAITING_DEADLINE_TEXT: [
                MessageHandler(_text_not_cmd, received_deadline_text_cb),
            ],
            CREATE_AWAITING_EDIT_TITLE: [
                MessageHandler(_text_not_cmd, received_edit_title_cb),
            ],
            CREATE_AWAITING_DESCRIPTION: [
                MessageHandler(_text_not_cmd, received_description_text_cb),
            ],
        },
        fallbacks=[
            # Известные текстовые команды — выходим из диалога и обрабатываем
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & _MAIN_COMMANDS_FILTER,
                conv_fallback_text_cb,
            ),
            CommandHandler("cancel", conv_cancel_cb),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=True,  # позволяет войти заново нажав другую кнопку
    )

    # ══════════════════════════════════════════════════════════════════════════
    # Диалог переноса дедлайна после переноса критической задачи в «На контроле»
    # Вызывается из text_handler при наличии context.user_data["pending_deadline_reschedule"].
    # Работает для ОБОИХ путей: кнопка «Перенести» и текстовая команда «перенести».

    async def received_deadline_reschedule_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Получен ответ на вопрос о переносе дедлайна задачи, переведённой в «На контроле».

        Варианты ответа пользователя:
          «.»         — оставить дедлайн без изменений
          «отменить»  — убрать дедлайн с карточки (due_date=None)
          любой текст — распарсить как дату и обновить due_date через update_card
        """
        assert update.message is not None

        pending = context.user_data.pop("pending_deadline_reschedule", None)
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None

        if user_ctx is None or pending is None:
            logger.warning(
                "received_deadline_reschedule_cb: нет pending ({}) или нет user_ctx ({})",
                pending, user_ctx,
            )
            return

        card_id = pending["card_id"]
        title   = pending.get("title", f"#{card_id}")
        text    = update.message.text.strip()

        if text == ".":
            # Оставить дедлайн как есть
            await update.message.reply_text(
                f"↩️ Дедлайн задачи «{title}» оставлен без изменений."
            )

        elif text.lower() == "отменить":
            # Убрать дедлайн
            try:
                await user_ctx.kaiten.update_card(card_id, due_date=None)
                await update.message.reply_text(f"🗑 Дедлайн задачи «{title}» убран.")
            except Exception as exc:
                logger.exception(
                    "received_deadline_reschedule_cb: update_card due_date=None — {}", exc
                )
                await update.message.reply_text("⚠️ Не удалось убрать дедлайн.")

        else:
            # Распарсить дату и обновить дедлайн
            try:
                intent = await cfg.claude.parse_intent(f"создать задачу дедлайн {text}")
            except Exception as exc:
                logger.exception(
                    "received_deadline_reschedule_cb: parse_intent — {}", exc
                )
                await update.message.reply_text(
                    "⚠️ Не удалось разобрать дату. Дедлайн не изменён."
                )
                if update.effective_chat:
                    await _resend_card_buttons(user_ctx, context, update.effective_chat.id)
                return

            deadline = intent.get("deadline")
            if not deadline:
                await update.message.reply_text(
                    "❓ Не удалось определить дату. Дедлайн не изменён."
                )
            else:
                try:
                    await user_ctx.kaiten.update_card(
                        card_id, due_date=f"{deadline}T00:00:00.000Z"
                    )
                    await update.message.reply_text(
                        f"✅ Дедлайн задачи «{title}» перенесён на {deadline}."
                    )
                    logger.info(
                        "received_deadline_reschedule_cb: card_id={} due_date={}", card_id, deadline
                    )
                except Exception as exc:
                    logger.exception(
                        "received_deadline_reschedule_cb: update_card due_date — {}", exc
                    )
                    await update.message.reply_text("⚠️ Не удалось обновить дедлайн.")

        if update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)

    # ══════════════════════════════════════════════════════════════════════════
    # Основной text_handler (роутер)

    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Роутер текстовых сообщений вне диалога."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("text_handler: unauthorized chat_id={}", chat_id)
            return

        text  = update.message.text.strip()
        lower = text.lower()

        logger.info(
            "text_handler: chat_id={} text={!r}",
            chat_id,
            text[:80],
        )

        # Ожидание ответа на вопрос о дедлайне после переноса в «На контроле».
        # Проверяем ДО роутинга команд — любой текст пользователя в этот момент
        # является ответом на этот вопрос (дата / «.» / «отменить»).
        if context.user_data.get("pending_deadline_reschedule"):
            await received_deadline_reschedule_cb(update, context)
            return

        if lower == "утро":
            await _handle_morning(update, user_ctx, context)
            return

        if lower == "вечер":
            await _handle_evening(update, user_ctx)
            return

        if lower in ("пересобрать", "перепланируй"):
            await _handle_replan(update, user_ctx, context)
            return

        if re.match(r"^(создать|создай)\b", lower):
            await _handle_create(update, cfg, _strip_command_prefix(text, "создать", "создай"), user_ctx)
            return

        if re.match(r"^(готово|выполнено|сделал|сделано)\b", lower):
            await _handle_done(
                update, cfg,
                _strip_command_prefix(text, "готово", "выполнено", "сделал", "сделано"),
                user_ctx,
            )
            return

        if re.match(r"^(перенести|перенеси|переместить)\b", lower):
            await _handle_move(
                update, cfg,
                _strip_command_prefix(text, "перенести", "перенеси", "переместить"),
                user_ctx,
                context,
            )
            return

        if re.match(r"^(заметка|комментарий|добавь заметку)\b", lower):
            await _handle_note(
                update, cfg,
                _strip_command_prefix(text, "заметка", "комментарий", "добавь заметку"),
                user_ctx,
            )
            return

        logger.debug("text_handler: неизвестная команда {!r}", text[:80])
        await _reply(update, HELP_TEXT)

    # ── Команды со слешем ─────────────────────────────────────────────────────

    async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/add <описание> — создать карточку."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("cmd_add: unauthorized chat_id={}", chat_id)
            return
        await _handle_create(update, cfg, " ".join(context.args or []), user_ctx)

    async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/done <описание> — архивировать карточку."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("cmd_done: unauthorized chat_id={}", chat_id)
            return
        await _handle_done(update, cfg, " ".join(context.args or []), user_ctx)

    async def cmd_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/move <описание> <куда> — переместить карточку."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("cmd_move: unauthorized chat_id={}", chat_id)
            return
        await _handle_move(update, cfg, " ".join(context.args or []), user_ctx, context)

    async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/note <название> // <заметка> — добавить комментарий."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("cmd_note: unauthorized chat_id={}", chat_id)
            return
        await _handle_note(update, cfg, " ".join(context.args or []), user_ctx)

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/help — показать список команд."""
        await _reply(update, HELP_TEXT)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start — приветствие."""
        await _reply(update, "👋 Привет! Я твой планировщик.\n\n" + HELP_TEXT)

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/cancel вне диалога — просто подтверждаем."""
        if update.message:
            await update.message.reply_text("Нечего отменять.")

    async def cmd_replan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/replan — пересобрать план дня с текущего момента."""
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("cmd_replan: unauthorized chat_id={}", chat_id)
            return
        await _handle_replan(update, user_ctx, context)

    # ── Предложение пересборки после создания задачи на сегодня ─────────────────
    # Top-level хендлеры для replan_offer:yes / replan_offer:no.
    # Работают из обеих точек входа: _finalize_new_task (конструктор /newtask)
    # и _handle_create (текстовая команда «создать»).

    async def replan_offer_yes_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Пользователь подтвердил пересборку плана после создания задачи на сегодня."""
        query = update.callback_query
        assert query is not None
        await query.answer()

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            await query.edit_message_text("⚠️ Не удалось определить пользователя.")
            return

        await query.edit_message_text("🔄 Пересобираю план с текущего момента…")
        try:
            plan_text = await user_ctx.replan_routine()
        except Exception as exc:
            logger.exception("replan_offer_yes_cb: ошибка — {}", exc)
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Не удалось пересобрать план. Попробуй позже.",
            )
            return

        sent_msg = None
        if plan_text:
            for part in _split_text(plan_text):
                try:
                    sent_msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=part,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_notification=True,
                    )
                except Exception:
                    try:
                        sent_msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=part,
                            disable_notification=True,
                        )
                    except Exception as exc2:
                        logger.error("replan_offer_yes_cb: ошибка отправки части — {}", exc2)
            if sent_msg and update.effective_chat:
                try:
                    await update.effective_chat.unpin_all_messages()
                except Exception as exc:
                    logger.warning("replan_offer_yes_cb: unpin_all_messages error — {}", exc)
                try:
                    await sent_msg.pin(disable_notification=True)
                except Exception as exc:
                    logger.warning("replan_offer_yes_cb: pin error — {}", exc)

        if update.effective_chat:
            context.user_data["view_ctx"] = {"scope": "today", "page": 0}
            try:
                today_col_id = user_ctx.logic.get_today_column_id()
                today_cards = await user_ctx.kaiten.get_cards(today_col_id)
                if today_cards:
                    await send_card_buttons(
                        today_cards, context.bot, chat_id,
                        page=0, silent=True, context=context,
                    )
            except Exception as exc:
                logger.warning(
                    "replan_offer_yes_cb: не удалось отправить кнопки карточек — {}", exc
                )

    async def replan_offer_no_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Пользователь отказался от пересборки плана после создания задачи."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        await query.edit_message_text("↩️ Хорошо, расписание остаётся как есть.")

    # ── Регистрация в строгом порядке ─────────────────────────────────────────
    #
    # ConversationHandler — первым, чтобы перехватывать card:* коллбэки.
    # page_nav_cb — сразу за ним: перехватывает page:* коллбэки (вне диалога).
    # confirm_move_cb / confirm_cancel_cb — top-level хендлеры для risky-подтверждения;
    #   работают как из текстовой команды «перенести», так и после кнопки «Перенести».
    # replan_offer_yes_cb / replan_offer_no_cb — top-level хендлеры для предложения
    #   пересборки после создания задачи на сегодня; работают из обеих точек входа.
    # Когда пользователь НЕ в диалоге, conv_handler не трогает текстовые
    # сообщения (его entry_points реагируют только на card:* коллбэки),
    # и update проваливается к text_handler ниже.

    app.add_handler(conv_handler)                                                      # ConvHandler
    app.add_handler(CallbackQueryHandler(page_nav_cb, pattern=r"^page:\d+$"))          # пагинация
    app.add_handler(CallbackQueryHandler(confirm_move_cb,   pattern=r"^confirm:move$"))   # risky ОК
    app.add_handler(CallbackQueryHandler(confirm_cancel_cb, pattern=r"^confirm:cancel$")) # risky отмена
    app.add_handler(CallbackQueryHandler(oc_cb, pattern=r"^oc:"))                      # другие колонки
    app.add_handler(CallbackQueryHandler(replan_offer_yes_cb, pattern=r"^replan_offer:yes$"))  # пересобрать
    app.add_handler(CallbackQueryHandler(replan_offer_no_cb,  pattern=r"^replan_offer:no$"))   # не пересобрать

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("done",   cmd_done))
    app.add_handler(CommandHandler("move",   cmd_move))
    app.add_handler(CommandHandler("note",   cmd_note))
    app.add_handler(CommandHandler("other",  cmd_other))
    app.add_handler(CommandHandler("replan", cmd_replan))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("build_handlers: все хендлеры зарегистрированы (включая ConversationHandler)")
    return app
