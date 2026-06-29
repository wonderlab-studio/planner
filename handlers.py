"""
handlers.py — хендлеры команд Telegram-бота.

Обрабатывает текстовые сообщения, команды со слешем
и интерактивные кнопки карточек (InlineKeyboard + ConversationHandler).

Стек: python-telegram-bot v20+ (async), loguru.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Awaitable

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

from board_logic import BoardLogic, COLUMN_IDS, COLUMN_NAME_BY_ID, WEEKDAY_COLUMNS
from claude_client import ClaudeClient
from kaiten_client import Card, KaitenClient, TAG_IDS
from notifier import Notifier

# ── Тип для routine-коллбэков ─────────────────────────────────────────────────

RoutineCallable = Callable[[], Awaitable[str]]

# ── Состояния ConversationHandler ─────────────────────────────────────────────

CARD_ACTION          = 0  # пользователь выбрал карточку, ждём действие
AWAITING_COMMENT     = 1  # ждём текст комментария (для done или comment)
AWAITING_HOURS       = 2  # ждём число часов для «Все на сегодня»
AWAITING_MOVE_TARGET = 3  # ждём куда перенести
AWAITING_QUESTION    = 4  # ждём вопрос для кнопки «Совет»
AWAITING_REMINDER_TIME = 5  # ждём дату/время для кнопки «Напоминалка»

# ── Фильтр «известные команды» — чтобы они не попадали в состояния диалога ────

_MAIN_COMMANDS_FILTER = filters.Regex(
    r"(?i)^(утро|вечер|создать|создай|готово|выполнено|сделал|сделано"
    r"|перенести|перенеси|переместить|заметка|комментарий)\b"
)

# ── Лимиты ────────────────────────────────────────────────────────────────────

_MAX_TG_LEN      = 4096   # символов в одном сообщении Telegram
MAX_CARD_BUTTONS = 20     # максимум кнопок в списке карточек
_BTN_TITLE_LEN   = 40     # символов в тексте кнопки

# ── Текст подсказки ───────────────────────────────────────────────────────────

HELP_TEXT = """\
📋 *Команды планировщика:*

*Ключевые слова:*
• `утро` — план на день + кнопки карточек
• `вечер` — итог дня

*Задачи:*
• `создать <описание>` — создать карточку
• `создай <описание>` — то же самое
• `/add <описание>` — создать карточку

*Управление:*
• `готово <описание>` — архивировать карточку
• `/done <описание>` — архивировать карточку
• `перенести <описание> <куда>` — переместить карточку
• `/move <описание> <куда>` — переместить карточку
• `заметка <название> // <текст>` — добавить комментарий
• `/note <название> // <текст>` — добавить комментарий

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


async def _reply(update: Update, text: str, parse_mode: str = ParseMode.MARKDOWN) -> None:
    """Отправляет ответ пользователю.

    Длинные тексты (> 4096 символов) автоматически разбиваются на части.
    При ошибке Markdown повторяет без форматирования.
    """
    assert update.message is not None

    for part in _split_text(text):
        try:
            await update.message.reply_text(part, parse_mode=parse_mode)
        except Exception:
            try:
                await update.message.reply_text(part)
            except Exception as exc:
                logger.error("_reply: не удалось отправить часть сообщения — {}", exc)


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


async def _load_active_cards(user_ctx: UserHandlerCtx) -> list[dict]:
    """Загружает карточки из всех активных колонок (дни недели + спец-колонки).

    Возвращает список простых словарей для передачи в claude.search_card_by_title.
    """
    active_columns = [
        col_id for name, col_id in COLUMN_IDS.items()
        if name != "Архив"
    ]

    all_cards: list[dict] = []
    for col_id in active_columns:
        col_name = next(k for k, v in COLUMN_IDS.items() if v == col_id)
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
) -> None:
    """Отправляет сообщение с InlineKeyboard из карточек сегодняшнего дня.

    Каждая кнопка — одна карточка (название обрезается до 40 символов).
    Максимум 20 кнопок (первые 20 карточек из списка).
    callback_data = "card:{card.id}"
    """
    task_cards = [c for c in cards if not c.blocked and not c.archived]
    if not task_cards:
        logger.debug("send_card_buttons: нет карточек для отображения")
        return

    shown = task_cards[:MAX_CARD_BUTTONS]
    keyboard = [
        [InlineKeyboardButton(
            text=c.title[:_BTN_TITLE_LEN] + ("…" if len(c.title) > _BTN_TITLE_LEN else ""),
            callback_data=f"card:{c.id}",
        )]
        for c in shown
    ]
    markup = InlineKeyboardMarkup(keyboard)

    suffix = f" (показаны первые {MAX_CARD_BUTTONS})" if len(task_cards) > MAX_CARD_BUTTONS else ""
    text = f"📋 *Карточки на сегодня{suffix}:*"

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
        logger.debug("send_card_buttons: отправлено {} кнопок", len(shown))
    except Exception as exc:
        logger.error("send_card_buttons: ошибка отправки — {}", exc)


# ── Обработчики утро/вечер ────────────────────────────────────────────────────

async def _handle_morning(
    update: Update,
    user_ctx: UserHandlerCtx,
    context: ContextTypes.DEFAULT_TYPE | None = None,
) -> None:
    """Запускает утреннюю рутину, отправляет план и кнопки карточек.

    Если routine возвращает непустую строку — отвечает ею прямо в чат.
    Если пустую — значит scheduler уже отправил через Notifier, дубль не нужен.
    После плана всегда отправляет кнопки карточек сегодняшнего дня.
    """
    assert update.message is not None
    logger.info("handle_morning: запрос от пользователя")

    await update.message.reply_text("⏳ Составляю план дня…")
    try:
        plan_text = await user_ctx.morning_routine()
        if plan_text:
            await _reply(update, plan_text)
    except Exception as exc:
        logger.exception("handle_morning: ошибка — {}", exc)
        await _reply(update, "⚠️ Не удалось составить план. Попробуй позже.")
        return

    # ── Кнопки карточек ───────────────────────────────────────────────────────
    if context is not None and update.effective_chat:
        try:
            today_col_id = user_ctx.logic.get_today_column_id()
            today_cards = await user_ctx.kaiten.get_cards(today_col_id)
            if today_cards:
                await send_card_buttons(today_cards, context.bot, update.effective_chat.id)
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

    if column_name and column_name in COLUMN_IDS:
        column_id = COLUMN_IDS[column_name]
    else:
        column_id = user_ctx.logic.get_today_column_id()
        column_name = next((k for k, v in COLUMN_IDS.items() if v == column_id), "сегодня")

    try:
        sort_order = await user_ctx.logic.get_section_sort_order(column_id, section)
    except Exception as exc:
        logger.warning("handle_create: get_section_sort_order error — {}, используем 1.0", exc)
        sort_order = 1.0

    properties: dict | None = None
    if importance:
        from kaiten_client import IMPORTANCE_OPTIONS
        opt_id = IMPORTANCE_OPTIONS.get(importance)
        if opt_id:
            properties = {"id_590382": [opt_id]}

    due_date_iso: str | None = f"{deadline}T00:00:00.000Z" if deadline else None

    try:
        card = await user_ctx.kaiten.create_card(
            column_id=column_id,
            title=title,
            due_date=due_date_iso,
            sort_order=sort_order,
            properties=properties,
        )
    except Exception as exc:
        logger.exception("handle_create: create_card error — {}", exc)
        await _reply(update, "⚠️ Не удалось создать карточку. Попробуй позже.")
        return

    if card is None:
        await _reply(update, "⚠️ Kaiten не вернул карточку. Возможно, создание не удалось.")
        return

    parts = [f"✅ Карточка создана: *{card.title}*", f"📅 Колонка: {column_name} / {section}"]
    if deadline:
        parts.append(f"⏰ Дедлайн: {deadline}")
    if importance:
        parts.append(f"🔥 Важность: {importance}")
    await _reply(update, "\n".join(parts))


# ── Обработчик «готово» ───────────────────────────────────────────────────────

async def _handle_done(
    update: Update,
    cfg: HandlersConfig,
    raw_text: str,
    user_ctx: UserHandlerCtx,
) -> None:
    """Ищет карточку и архивирует её."""
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

    comment = f"Выполнено {date.today().isoformat()}"
    try:
        ok = await user_ctx.kaiten.archive_card(matched["id"], comment=comment)
    except Exception as exc:
        logger.exception("handle_done: archive_card error — {}", exc)
        await _reply(update, "⚠️ Не удалось архивировать карточку. Попробуй позже.")
        return

    if ok:
        await _reply(update, f"✅ Готово! Карточка «{matched['title']}» перемещена в архив.")
    else:
        await _reply(update, f"⚠️ Не удалось архивировать «{matched['title']}».")


# ── Обработчик «перенести» ────────────────────────────────────────────────────

async def _handle_move(
    update: Update,
    cfg: HandlersConfig,
    raw_text: str,
    user_ctx: UserHandlerCtx,
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

    if column_name and column_name in COLUMN_IDS:
        target_column_id   = COLUMN_IDS[column_name]
        target_column_name = column_name
    else:
        tomorrow_wd        = (date.today() + timedelta(days=1)).weekday()
        target_column_name = WEEKDAY_COLUMNS[tomorrow_wd]
        target_column_id   = COLUMN_IDS[target_column_name]

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

    if card:
        await _reply(
            update,
            f"📦 «{matched['title']}» перенесена → *{target_column_name} / {section}*",
        )
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
        [InlineKeyboardButton("✅ Готово",          callback_data="action:done")],
        [InlineKeyboardButton("➡️ Все на сегодня",  callback_data="action:today")],
        [InlineKeyboardButton("💬 Комментарий",     callback_data="action:comment")],
        [InlineKeyboardButton("📅 Перенести",       callback_data="action:move")],
        [InlineKeyboardButton("🤖 Совет",           callback_data="action:advice")],
        [InlineKeyboardButton("🔔 Напоминалка",     callback_data="action:reminder")],
        [InlineKeyboardButton("← Назад",            callback_data="action:back")],
    ])


async def _resend_card_buttons(
    user_ctx: UserHandlerCtx,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | str,
) -> None:
    """Повторно отправляет актуальные кнопки карточек сегодняшней колонки."""
    try:
        today_col_id = user_ctx.logic.get_today_column_id()
        cards = await user_ctx.kaiten.get_cards(today_col_id)
        task_cards = [c for c in cards if not c.blocked and not c.archived]
        if task_cards:
            await send_card_buttons(task_cards, context.bot, chat_id)
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Все карточки на сегодня обработаны.",
            )
    except Exception as exc:
        logger.warning("_resend_card_buttons: ошибка — {}", exc)


# ── Вспомогательная логика для кнопки «Все на сегодня» ───────────────────────

def _next_col_for_regular(card: Card, today: date) -> int:
    """Возвращает column_id следующего подходящего дня для регулярной задачи.

    Правила:
        еженедельно  → «Следующая неделя» (Monday run распределит по weekday-полю)
        ежедневно    → завтра
        по будням    → ближайший пн–пт начиная с завтра
        по выходным  → ближайший сб–вс начиная с завтра

    Для нерегулярных карточек возвращает завтра (нейтральный дефолт,
    вызывающий код должен проверять is_regular_task перед вызовом).
    """
    tags = set(card.tag_ids)

    # Еженедельные → следующая неделя: Monday run поставит в нужный день по weekday-полю
    if TAG_IDS["еженедельно"] in tags:
        return COLUMN_IDS["Следующая неделя"]

    # Для остальных ищем ближайший подходящий день (максимум 7 шагов вперёд)
    for days_ahead in range(1, 8):
        candidate = today + timedelta(days=days_ahead)
        wd = candidate.weekday()  # 0=Пн … 6=Вс

        if TAG_IDS["ежедневно"] in tags:
            break                                        # любой день — берём завтра

        if TAG_IDS["по будням"] in tags and wd <= 4:
            break                                        # нашли ближайший будний

        if TAG_IDS["по выходным"] in tags and wd >= 5:
            break                                        # нашли ближайний выходной
    else:
        # Теоретически недостижимо для корректных тегов, но страхуемся
        logger.warning("_next_col_for_regular: не нашли слот за 7 дней, card_id={}", card.id)
        return COLUMN_IDS["Следующая неделя"]

    target_wd = (today + timedelta(days=days_ahead)).weekday()
    return COLUMN_IDS[WEEKDAY_COLUMNS[target_wd]]


# ── Фабрика хендлеров ─────────────────────────────────────────────────────────

def build_handlers(cfg: HandlersConfig) -> Application:
    """Создаёт и настраивает Application с зарегистрированными хендлерами.

    Порядок регистрации (важно!):
        1. ConversationHandler — group=0, первым: перехватывает card:* коллбэки
           и ведёт диалог выбора действия над карточкой.
        2. CommandHandlers — group=0.
        3. MessageHandler (text_handler) — group=0, последним: роутер текста.

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
        try:
            card = await user_ctx.kaiten.get_card(card_id)
            title = card.title if card else f"Карточка #{card_id}"
        except Exception:
            title = f"Карточка #{card_id}"

        context.user_data["selected_card_title"] = title

        await query.edit_message_text(
            f"*{title}*\n\nЧто делаем?",
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
        await query.edit_message_text(
            "💬 Добавь комментарий к выполненной задаче\n"
            "_(или напиши «-» чтобы архивировать без комментария)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
        )
        return AWAITING_COMMENT

    async def action_today_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """➡️ Все на сегодня → запрашиваем число часов, потом перенесём."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        await query.edit_message_text(
            "⏱ Сколько часов займёт задача? _(введи целое число)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
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
        await query.edit_message_text(
            "💬 Введи текст комментария:",
            reply_markup=None,
        )
        return AWAITING_COMMENT

    async def action_move_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """📅 Перенести → запрашиваем куда."""
        query = update.callback_query
        assert query is not None
        await query.answer()
        await query.edit_message_text(
            "📅 Куда перенести?\n"
            "_Например: «завтра», «пятница вечер», «следующая неделя»_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
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
        """Получен текст комментария: архивируем или добавляем комментарий."""
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

        # «-» = пропустить комментарий (только для done)
        comment_text: str | None = None if text == "-" else text

        if action == "done":
            try:
                ok = await user_ctx.kaiten.archive_card(card_id, comment=comment_text or None)
                if ok:
                    await update.message.reply_text(
                        f"✅ «{title}» выполнено и перемещено в архив."
                    )
                else:
                    await update.message.reply_text(
                        f"⚠️ Не удалось архивировать «{title}»."
                    )
            except Exception as exc:
                logger.exception("received_comment_cb: archive error — {}", exc)
                await update.message.reply_text("⚠️ Ошибка при архивации.")

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
        """Получено число часов: обновляем size и переносим на следующий слот."""
        assert update.message is not None

        chat_id = update.effective_chat.id if update.effective_chat else None
        user_ctx = cfg.users.get(chat_id) if chat_id else None
        if user_ctx is None:
            logger.warning("received_hours_cb: unauthorized chat_id={}", chat_id)
            return ConversationHandler.END

        text    = update.message.text.strip()
        card_id = context.user_data.get("selected_card_id")
        title   = context.user_data.get("selected_card_title", f"#{card_id}")

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

        # Обновляем size
        try:
            await user_ctx.kaiten.update_card(card_id, size=hours)
            logger.info("received_hours_cb: card_id={} size={}", card_id, hours)
        except Exception as exc:
            logger.exception("received_hours_cb: update_card error — {}", exc)
            await update.message.reply_text("⚠️ Не удалось обновить размер задачи.")
            return ConversationHandler.END

        # Ищем следующий подходящий слот с учётом типа задачи
        try:
            card = await user_ctx.kaiten.get_card(card_id)
            if card is None:
                raise ValueError(f"card {card_id} not found after update")

            # Для регулярных задач — стартуем с колонки по метке,
            # для обычных — с завтра.
            if user_ctx.logic.is_regular_task(card):
                start_col_id = _next_col_for_regular(card, date.today())
            else:
                tomorrow     = date.today() + timedelta(days=1)
                start_col_id = COLUMN_IDS[WEEKDAY_COLUMNS[tomorrow.weekday()]]

            slot = await user_ctx.logic.find_slot_for_card(card, start_col_id)
            if slot is None:
                slot = (COLUMN_IDS["Следующая неделя"], "Утро")

            target_col_id, target_section = slot
            sort_order = await user_ctx.logic.get_section_sort_order(target_col_id, target_section)
            await user_ctx.kaiten.move_card(card_id, target_col_id, sort_order)

            target_col_name = COLUMN_NAME_BY_ID.get(target_col_id, str(target_col_id))
            await update.message.reply_text(
                f"✅ «{title}»: размер {hours} ч → *{target_col_name} / {target_section}*",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.exception("received_hours_cb: move error — {}", exc)
            await update.message.reply_text(
                f"✅ Размер обновлён ({hours} ч).\n⚠️ Не удалось перенести карточку."
            )

        if update.effective_chat:
            await _resend_card_buttons(user_ctx, context, update.effective_chat.id)

        return ConversationHandler.END

    async def received_move_target_cb(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Получен текст с целью переноса: парсим через Claude и двигаем карточку."""
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
        section     = intent.get("section") or "Утро"

        if column_name and column_name in COLUMN_IDS:
            target_col_id   = COLUMN_IDS[column_name]
            target_col_name = column_name
        else:
            tomorrow        = date.today() + timedelta(days=1)
            target_col_name = WEEKDAY_COLUMNS[tomorrow.weekday()]
            target_col_id   = COLUMN_IDS[target_col_name]

        try:
            sort_order = await user_ctx.logic.get_section_sort_order(target_col_id, section)
            moved = await user_ctx.kaiten.move_card(card_id, target_col_id, sort_order)
            if moved:
                await update.message.reply_text(
                    f"📦 «{title}» → *{target_col_name} / {section}*",
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                await update.message.reply_text(f"⚠️ Не удалось переместить «{title}».")
        except Exception as exc:
            logger.exception("received_move_target_cb: move error — {}", exc)
            await update.message.reply_text("⚠️ Ошибка при перемещении карточки.")

        if update.effective_chat:
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
        await query.edit_message_text(
            "🤖 Какой вопрос по этой задаче?\n"
            "_Например: «с чего начать», «какие риски», «что учесть»_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
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
                title=title,
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
        await query.edit_message_text(
            "🔔 Когда напомнить?\n"
            "_Например: «завтра в 14:00», «пятница 09:30», «20 июня 10:00»_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=None,
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
        remind_tag = TAG_IDS["напомнить"]
        try:
            card = await user_ctx.kaiten.get_card(card_id)
            current_tags = list(card.tag_ids) if card else []
            if remind_tag not in current_tags:
                await user_ctx.kaiten.add_tag(card_id, remind_tag)
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
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("↩️ Действие отменено.")
        return ConversationHandler.END

    # ── Сборка ConversationHandler ────────────────────────────────────────────

    # Текстовый фильтр для состояний ожидания: не реагирует на известные команды
    _text_not_cmd = filters.TEXT & ~filters.COMMAND & ~_MAIN_COMMANDS_FILTER

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(card_selected_cb, pattern=r"^card:\d+$"),
        ],
        states={
            CARD_ACTION: [
                CallbackQueryHandler(action_done_cb,     pattern=r"^action:done$"),
                CallbackQueryHandler(action_today_cb,    pattern=r"^action:today$"),
                CallbackQueryHandler(action_comment_cb,  pattern=r"^action:comment$"),
                CallbackQueryHandler(action_move_cb,     pattern=r"^action:move$"),
                CallbackQueryHandler(action_advice_cb,   pattern=r"^action:advice$"),
                CallbackQueryHandler(action_reminder_cb, pattern=r"^action:reminder$"),
                CallbackQueryHandler(action_back_cb,     pattern=r"^action:back$"),
            ],
            AWAITING_COMMENT: [
                MessageHandler(_text_not_cmd, received_comment_cb),
            ],
            AWAITING_HOURS: [
                MessageHandler(_text_not_cmd, received_hours_cb),
            ],
            AWAITING_MOVE_TARGET: [
                MessageHandler(_text_not_cmd, received_move_target_cb),
            ],
            AWAITING_QUESTION: [
                MessageHandler(_text_not_cmd, received_question_cb),
            ],
            AWAITING_REMINDER_TIME: [
                MessageHandler(_text_not_cmd, received_reminder_time_cb),
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
        allow_reentry=True,  # позволяет войти заново нажав другую кнопку
    )

    # ══════════════════════════════════════════════════════════════════════════
    # Основной text_handler (роутер)
    # ══════════════════════════════════════════════════════════════════════════

    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Главный роутер текстовых сообщений."""
        if update.message is None or update.message.text is None:
            return

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

        if lower == "утро":
            await _handle_morning(update, user_ctx, context)
            return

        if lower == "вечер":
            await _handle_evening(update, user_ctx)
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
        await _handle_move(update, cfg, " ".join(context.args or []), user_ctx)

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

    # ── Регистрация в строгом порядке ─────────────────────────────────────────
    #
    # ConversationHandler — первым, чтобы перехватывать card:* коллбэки.
    # Когда пользователь НЕ в диалоге, conv_handler не трогает текстовые
    # сообщения (его entry_points реагируют только на card:* коллбэки),
    # и update проваливается к text_handler ниже.

    app.add_handler(conv_handler)                                           # ConvHandler

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("done",   cmd_done))
    app.add_handler(CommandHandler("move",   cmd_move))
    app.add_handler(CommandHandler("note",   cmd_note))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("build_handlers: все хендлеры зарегистрированы (включая ConversationHandler)")
    return app
