"""
handlers.py — хендлеры команд Telegram-бота.

Обрабатывает текстовые сообщения и команды со слешем.
Зависимости передаются через HandlersConfig — датакласс с инжекцией.

Стек: python-telegram-bot v20+ (async), loguru.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Awaitable

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

from kaiten_client import KaitenClient
from board_logic import BoardLogic, COLUMN_IDS
from claude_client import ClaudeClient
from notifier import Notifier

# ── Тип для routine-коллбэков ─────────────────────────────────────────────────

RoutineCallable = Callable[[], Awaitable[str]]

# ── Текст подсказки ───────────────────────────────────────────────────────────

HELP_TEXT = """\
📋 *Команды планировщика:*

*Ключевые слова:*
• `утро` — план на день
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
• `заметка <описание> <текст>` — добавить комментарий
• `/note <описание> <текст>` — добавить комментарий

_Описание задачи может быть приблизительным — я найду нужную карточку._\
"""

# ── HandlersConfig ────────────────────────────────────────────────────────────


@dataclass
class HandlersConfig:
    """Зависимости для всех хендлеров. Передаётся при регистрации."""

    kaiten: KaitenClient
    logic: BoardLogic
    claude: ClaudeClient
    notifier: Notifier
    morning_routine: RoutineCallable
    evening_routine: RoutineCallable


# ── Вспомогательные функции ───────────────────────────────────────────────────

async def _reply(update: Update, text: str, parse_mode: str = ParseMode.MARKDOWN) -> None:
    """Отправляет ответ пользователю. При ошибке Markdown повторяет без форматирования."""
    assert update.message is not None
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception:
        # Markdown-ошибка или иное — пробуем без форматирования
        try:
            await update.message.reply_text(text)
        except Exception as exc:
            logger.error("_reply: не удалось отправить ответ — {}", exc)


def _normalize(text: str) -> str:
    """Нормализует текст: убирает лишние пробелы, приводит к нижнему регистру."""
    return text.strip().lower()


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


async def _load_active_cards(cfg: HandlersConfig) -> list[dict]:
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
            cards = await cfg.kaiten.get_cards(col_id)
            for card in cards:
                if card.blocked:
                    continue  # разделители не нужны
                all_cards.append({
                    "id":     card.id,
                    "title":  card.title,
                    "column": col_name,
                })
        except Exception as exc:
            logger.warning("_load_active_cards: ошибка при загрузке колонки {} — {}", col_id, exc)

    logger.debug("_load_active_cards: загружено {} карточек", len(all_cards))
    return all_cards


# ── Обработчики утро/вечер ────────────────────────────────────────────────────

async def _handle_morning(update: Update, cfg: HandlersConfig) -> None:
    """Запускает утреннюю рутину и отправляет план дня.

    Если routine возвращает непустую строку — отвечает ею прямо в чат.
    Если пустую — значит scheduler уже отправил через Notifier, дубль не нужен.
    """
    assert update.message is not None
    logger.info("handle_morning: запрос от пользователя")

    await update.message.reply_text("⏳ Составляю план дня…")
    try:
        plan_text = await cfg.morning_routine()
        if plan_text:
            await _reply(update, plan_text)
    except Exception as exc:
        logger.exception("handle_morning: ошибка — {}", exc)
        await _reply(update, "⚠️ Не удалось составить план. Попробуй позже.")


async def _handle_evening(update: Update, cfg: HandlersConfig) -> None:
    """Запускает вечернюю рутину и отправляет итог дня.

    Если routine возвращает непустую строку — отвечает ею прямо в чат.
    Если пустую — значит scheduler уже отправил через Notifier, дубль не нужен.
    """
    assert update.message is not None
    logger.info("handle_evening: запрос от пользователя")

    await update.message.reply_text("⏳ Подвожу итоги дня…")
    try:
        summary_text = await cfg.evening_routine()
        if summary_text:
            await _reply(update, summary_text)
    except Exception as exc:
        logger.exception("handle_evening: ошибка — {}", exc)
        await _reply(update, "⚠️ Не удалось подвести итог. Попробуй позже.")


# ── Обработчик «создать» ──────────────────────────────────────────────────────

async def _handle_create(update: Update, cfg: HandlersConfig, raw_text: str) -> None:
    """Парсит намерение и создаёт карточку в Kaiten."""
    assert update.message is not None
    logger.info("handle_create: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(update, "❓ Укажи описание задачи. Например: `создать Купить молоко завтра`")
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

    title = intent.get("title") or raw_text
    column_name: str | None = intent.get("column")
    section: str | None = intent.get("section") or "Утро"
    deadline: str | None = intent.get("deadline")
    importance: str | None = intent.get("importance")

    # Определяем колонку
    if column_name and column_name in COLUMN_IDS:
        column_id = COLUMN_IDS[column_name]
    else:
        # По умолчанию — сегодня
        column_id = cfg.logic.get_today_column_id()
        column_name = next(
            (k for k, v in COLUMN_IDS.items() if v == column_id), "сегодня"
        )

    try:
        sort_order = await cfg.logic.get_section_sort_order(column_id, section)
    except Exception as exc:
        logger.warning("handle_create: get_section_sort_order error — {}, используем 1.0", exc)
        sort_order = 1.0

    # Формируем properties если есть importance
    properties: dict | None = None
    if importance:
        from kaiten_client import IMPORTANCE_OPTIONS
        opt_id = IMPORTANCE_OPTIONS.get(importance)
        if opt_id:
            properties = {"id_590382": [opt_id]}

    # due_date в ISO формат с временем
    due_date_iso: str | None = None
    if deadline:
        due_date_iso = f"{deadline}T00:00:00.000Z"

    try:
        card = await cfg.kaiten.create_card(
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

    # Формируем ответ
    parts = [f"✅ Карточка создана: *{card.title}*"]
    parts.append(f"📅 Колонка: {column_name} / {section}")
    if deadline:
        parts.append(f"⏰ Дедлайн: {deadline}")
    if importance:
        parts.append(f"🔥 Важность: {importance}")

    await _reply(update, "\n".join(parts))


# ── Обработчик «готово» ───────────────────────────────────────────────────────

async def _handle_done(update: Update, cfg: HandlersConfig, raw_text: str) -> None:
    """Ищет карточку и архивирует её."""
    assert update.message is not None
    logger.info("handle_done: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(update, "❓ Укажи название задачи. Например: `готово купить молоко`")
        return

    await update.message.reply_text("🔍 Ищу карточку…")

    try:
        all_cards = await _load_active_cards(cfg)
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
        ok = await cfg.kaiten.archive_card(matched["id"], comment=comment)
    except Exception as exc:
        logger.exception("handle_done: archive_card error — {}", exc)
        await _reply(update, "⚠️ Не удалось архивировать карточку. Попробуй позже.")
        return

    if ok:
        await _reply(update, f"✅ Готово! Карточка «{matched['title']}» перемещена в архив.")
    else:
        await _reply(update, f"⚠️ Не удалось архивировать «{matched['title']}».")


# ── Обработчик «перенести» ────────────────────────────────────────────────────

async def _handle_move(update: Update, cfg: HandlersConfig, raw_text: str) -> None:
    """Ищет карточку и перемещает её в нужную колонку/секцию."""
    assert update.message is not None
    logger.info("handle_move: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(
            update,
            "❓ Укажи задачу и куда перенести. Например: `перенести молоко на завтра`",
        )
        return

    # Парсим намерение — там будут column и section
    try:
        intent = await cfg.claude.parse_intent(raw_text)
    except Exception as exc:
        logger.exception("handle_move: parse_intent error — {}", exc)
        await _reply(update, "⚠️ Не удалось разобрать команду.")
        return

    query = intent.get("title") or raw_text
    column_name: str | None = intent.get("column")
    section: str | None = intent.get("section") or "Утро"

    await update.message.reply_text("🔍 Ищу карточку…")

    try:
        all_cards = await _load_active_cards(cfg)
        matched = await cfg.claude.search_card_by_title(query, all_cards)
    except Exception as exc:
        logger.exception("handle_move: поиск — {}", exc)
        await _reply(update, "⚠️ Ошибка при поиске карточки.")
        return

    if matched is None:
        await _reply(update, f"❓ Не нашёл карточку по запросу «{query}».")
        return

    # Определяем целевую колонку
    if column_name and column_name in COLUMN_IDS:
        target_column_id = COLUMN_IDS[column_name]
        target_column_name = column_name
    else:
        # Нет явного указания — переносим на завтра
        from datetime import timedelta
        tomorrow_wd = (date.today() + timedelta(days=1)).weekday()
        from board_logic import WEEKDAY_COLUMNS
        target_column_name = WEEKDAY_COLUMNS[tomorrow_wd]
        target_column_id = COLUMN_IDS[target_column_name]

    try:
        sort_order = await cfg.logic.get_section_sort_order(target_column_id, section)
    except Exception as exc:
        logger.warning("handle_move: get_section_sort_order — {}, используем 1.0", exc)
        sort_order = 1.0

    try:
        card = await cfg.kaiten.move_card(matched["id"], target_column_id, sort_order)
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

async def _handle_note(update: Update, cfg: HandlersConfig, raw_text: str) -> None:
    """Ищет карточку и добавляет к ней комментарий."""
    assert update.message is not None
    logger.info("handle_note: raw_text={!r}", raw_text)

    if not raw_text:
        await _reply(
            update,
            "❓ Укажи задачу и текст заметки. Например: `заметка молоко купить 2 литра`",
        )
        return

    try:
        intent = await cfg.claude.parse_intent(raw_text)
    except Exception as exc:
        logger.exception("handle_note: parse_intent error — {}", exc)
        await _reply(update, "⚠️ Не удалось разобрать команду.")
        return

    query    = intent.get("title") or raw_text
    note_text = intent.get("note") or raw_text

    await update.message.reply_text("🔍 Ищу карточку…")

    try:
        all_cards = await _load_active_cards(cfg)
        matched = await cfg.claude.search_card_by_title(query, all_cards)
    except Exception as exc:
        logger.exception("handle_note: поиск — {}", exc)
        await _reply(update, "⚠️ Ошибка при поиске карточки.")
        return

    if matched is None:
        await _reply(update, f"❓ Не нашёл карточку по запросу «{query}».")
        return

    try:
        ok = await cfg.kaiten.add_comment(matched["id"], note_text)
    except Exception as exc:
        logger.exception("handle_note: add_comment error — {}", exc)
        await _reply(update, "⚠️ Не удалось добавить заметку.")
        return

    if ok:
        await _reply(update, f"📝 Заметка добавлена к «{matched['title']}».")
    else:
        await _reply(update, f"⚠️ Не удалось добавить заметку к «{matched['title']}».")


# ── Фабрика хендлеров ─────────────────────────────────────────────────────────

def build_handlers(cfg: HandlersConfig) -> Application:
    """Создаёт и настраивает Application с зарегистрированными хендлерами.

    Параметры:
        cfg — датакласс с зависимостями (kaiten, logic, claude, notifier, routines)

    Возвращает:
        Готовый Application, который можно запустить через app.run_polling().

    Пример использования в bot.py:
        cfg = HandlersConfig(kaiten=..., logic=..., claude=..., notifier=...,
                             morning_routine=morning_routine,
                             evening_routine=evening_routine)
        app = build_handlers(cfg)
        app.run_polling()
    """
    from telegram.ext import ApplicationBuilder
    import os
    from dotenv import load_dotenv
    load_dotenv()

    token = os.getenv("TELEGRAM_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_TOKEN не задан в .env")

    app = ApplicationBuilder().token(token).build()

    # ── Текстовые сообщения ───────────────────────────────────────────────────

    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Главный роутер текстовых сообщений."""
        if update.message is None or update.message.text is None:
            return

        text = update.message.text.strip()
        lower = text.lower()

        logger.info(
            "text_handler: chat_id={} text={!r}",
            update.effective_chat.id if update.effective_chat else "?",
            text[:80],
        )

        # ── Утро / Вечер ──────────────────────────────────────────────────────
        if lower == "утро":
            await _handle_morning(update, cfg)
            return

        if lower == "вечер":
            await _handle_evening(update, cfg)
            return

        # ── Создать ───────────────────────────────────────────────────────────
        if re.match(r"^(создать|создай)\b", lower):
            payload = _strip_command_prefix(text, "создать", "создай")
            await _handle_create(update, cfg, payload)
            return

        # ── Готово ────────────────────────────────────────────────────────────
        if re.match(r"^(готово|выполнено|сделал|сделано)\b", lower):
            payload = _strip_command_prefix(text, "готово", "выполнено", "сделал", "сделано")
            await _handle_done(update, cfg, payload)
            return

        # ── Перенести ─────────────────────────────────────────────────────────
        if re.match(r"^(перенести|перенеси|переместить)\b", lower):
            payload = _strip_command_prefix(text, "перенести", "перенеси", "переместить")
            await _handle_move(update, cfg, payload)
            return

        # ── Заметка ───────────────────────────────────────────────────────────
        if re.match(r"^(заметка|комментарий|добавь заметку)\b", lower):
            payload = _strip_command_prefix(text, "заметка", "комментарий", "добавь заметку")
            await _handle_note(update, cfg, payload)
            return

        # ── Неизвестное сообщение ─────────────────────────────────────────────
        logger.debug("text_handler: неизвестная команда {!r}", text[:80])
        await _reply(update, HELP_TEXT)

    # ── Команды со слешем ─────────────────────────────────────────────────────

    async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/add <описание> — создать карточку."""
        payload = " ".join(context.args or [])
        await _handle_create(update, cfg, payload)

    async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/done <описание> — архивировать карточку."""
        payload = " ".join(context.args or [])
        await _handle_done(update, cfg, payload)

    async def cmd_move(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/move <описание> <куда> — переместить карточку."""
        payload = " ".join(context.args or [])
        await _handle_move(update, cfg, payload)

    async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/note <описание> <заметка> — добавить комментарий."""
        payload = " ".join(context.args or [])
        await _handle_note(update, cfg, payload)

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/help — показать список команд."""
        await _reply(update, HELP_TEXT)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start — приветствие."""
        await _reply(
            update,
            "👋 Привет! Я твой планировщик.\n\n" + HELP_TEXT,
        )

    # ── Регистрация ───────────────────────────────────────────────────────────

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("add",   cmd_add))
    app.add_handler(CommandHandler("done",  cmd_done))
    app.add_handler(CommandHandler("move",  cmd_move))
    app.add_handler(CommandHandler("note",  cmd_note))

    # Текстовые сообщения (не команды) — ловим всё что не начинается с /
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("build_handlers: все хендлеры зарегистрированы")
    return app
