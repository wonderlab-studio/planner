"""
onboarding.py — self-service онбординг новых пользователей Telegram-бота.

Содержит:
- parse_kaiten_url  — чистая функция извлечения base_url и space_id из ссылки браузера
- _UnknownUser      — MessageFilter: True для chat_id, не зарегистрированного в cfg.users
- OnboardingService — провижининг: валидация токена, список пространств, создание доски
- build_onboarding_handler — собирает ConversationHandler онбординга

ВАЖНО: этот модуль НЕ импортирует handlers.py (иначе цикл импорта).
Строить UserHandlerCtx здесь нельзя — это делает инъектированный register_user из bot.py.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from board_setup import setup_board
from kaiten_client import KaitenClient
from notifier import Notifier
from user_config import UserConfig

# ── Состояния (100+ чтобы не пересекаться с основным ConversationHandler 0..12) ──

OB_AWAIT_URL   = 100
OB_AWAIT_TOKEN = 101
OB_AWAIT_SPACE = 102

# ── Тип коллбэка регистрации пользователя ────────────────────────────────────

RegisterUser = Callable[[UserConfig, KaitenClient], Awaitable[None]]


# ── Исключение ────────────────────────────────────────────────────────────────

class OnboardingError(Exception):
    pass


# ── Вспомогательная функция ───────────────────────────────────────────────────

def parse_kaiten_url(url: str) -> tuple[str | None, int | None]:
    """Из ссылки браузера Kaiten извлекает base_url API и (если есть) space_id.

    Примеры входа:
        https://acme.kaiten.ru/space/123456   → ('https://acme.kaiten.ru/api/latest', 123456)
        https://acme.kaiten.ru/123456         → (..., 123456)
        https://acme.kaiten.ru/ticket/42      → (..., None)

    Возвращает (None, None) если это не ссылка Kaiten.
    """
    url = (url or "").strip()
    m = re.search(r"https?://([a-zA-Z0-9\-]+\.kaiten\.ru)", url)
    if not m:
        return None, None
    base_url = f"https://{m.group(1)}/api/latest"
    sm = re.search(r"/space/(\d+)", url) or re.search(
        r"\.kaiten\.ru/(\d+)(?:[/?#]|$)", url
    )
    space_id = int(sm.group(1)) if sm else None
    return base_url, space_id


# ── Фильтр «неизвестный пользователь» ────────────────────────────────────────

class _UnknownUser(filters.MessageFilter):
    """True если chat_id ещё не зарегистрирован в cfg.users.

    Используется в entry_points онбординга — известные пользователи
    проваливаются к обычным хендлерам, минуя онбординг.
    """

    def __init__(self, cfg) -> None:
        super().__init__()
        self._cfg = cfg

    def filter(self, message) -> bool:
        return message.chat_id not in self._cfg.users


# ── Сервис провижининга ───────────────────────────────────────────────────────

class OnboardingService:
    """Выполняет всю логику онбординга: валидация, создание доски, регистрация."""

    def __init__(
        self,
        register_user: RegisterUser,
        owner_chat_id: int | None,
        loop,
    ) -> None:
        self._register_user = register_user
        self._owner_chat_id = owner_chat_id
        self._loop = loop

    def _gen_user_id(self, chat_id: int, username: str | None) -> str:
        """Генерирует уникальный user_id на основе username или chat_id."""
        base = re.sub(r"[^a-z0-9]", "", (username or "").lower()) or f"u{chat_id}"
        try:
            existing = {r["user_id"] for r in db.load_user_records()}
        except Exception:
            existing = set()
        uid, i = base, 1
        while uid in existing:
            uid = f"{base}{i}"
            i += 1
        return uid

    async def validate_token(
        self, base_url: str, token: str, space_id: int | None
    ) -> bool:
        """Проверяет токен через get_current_user. True — токен рабочий."""
        client = KaitenClient(
            board_id=0, lane_id=0, token=token,
            base_url=base_url, space_id=space_id or 0,
        )
        try:
            return (await client.get_current_user()) is not None
        finally:
            await client.close()

    async def list_spaces(self, base_url: str, token: str) -> list[dict]:
        """Возвращает список пространств пользователя."""
        client = KaitenClient(
            board_id=0, lane_id=0, token=token, base_url=base_url, space_id=0,
        )
        try:
            return await client.get_spaces()
        finally:
            await client.close()

    async def provision_user(
        self,
        chat_id: int,
        username: str | None,
        base_url: str,
        token: str,
        space_id: int,
    ) -> dict:
        """Полный провижининг пользователя:
        1. Генерирует user_id
        2. Создаёт доску «Планировщик» в Kaiten
        3. Запускает setup_board (колонки, разделители, кастомные поля)
        4. Сохраняет запись в SQLite
        5. Вызывает register_user (горячее подключение к scheduler и cfg.users)
        6. Уведомляет владельца о новом пользователе и его токене
        """
        user_id = self._gen_user_id(chat_id, username)
        token_env = f"KAITEN_TOKEN_{user_id.upper()}"

        # Создаём доску
        tmp = KaitenClient(
            board_id=0, lane_id=0, token=token,
            base_url=base_url, space_id=space_id,
        )
        try:
            board = await tmp.create_board(title="Планировщик")
        finally:
            await tmp.close()

        if not board or "id" not in board:
            raise OnboardingError("Kaiten не создал доску — проверь права токена")
        board_id = int(board["id"])

        # Собираем UserConfig с временными нулями (setup_board заполнит lane_id)
        user_cfg = UserConfig(
            user_id=user_id,
            telegram_chat_id=chat_id,
            kaiten_board_id=board_id,
            kaiten_lane_id=0,
            kaiten_space_id=space_id,
            kaiten_token=token,
            kaiten_base_url=base_url,
        )

        client = KaitenClient(
            board_id=board_id, lane_id=0, token=token,
            base_url=base_url, space_id=space_id,
        )

        # setup_board мутирует user_cfg.column_ids и user_cfg.kaiten_lane_id,
        # конфигурирует client._lane_id и прочие поля
        column_ids, discovered = await setup_board(
            client, user_cfg, needs_custom_fields=True
        )

        if discovered:
            await self._loop.run_in_executor(
                None, db.save_user_kaiten_config, user_id, discovered
            )

        await self._loop.run_in_executor(
            None,
            db.save_user_record,
            user_id,
            chat_id,
            board_id,
            user_cfg.kaiten_lane_id,
            space_id,
            token_env,
            base_url,
            user_cfg.column_ids,
            user_cfg.timezone,
        )

        # Горячее подключение: строит логику/morning/notifier/handler-ctx,
        # добавляет пользователя в scheduler и cfg.users
        await self._register_user(user_cfg, client)

        await self._notify_owner(
            user_id, token_env, token, chat_id, board_id, base_url, space_id
        )
        logger.info(
            "onboarding: пользователь user={} подключён, board_id={}", user_id, board_id
        )
        return {"user_id": user_id, "board_id": board_id, "token_env": token_env}

    async def _notify_owner(
        self,
        user_id: str,
        token_env: str,
        token: str,
        chat_id: int,
        board_id: int,
        base_url: str,
        space_id: int,
    ) -> None:
        """Пересылает владельцу токен нового пользователя и инструкцию по env."""
        if not self._owner_chat_id:
            logger.warning(
                "onboarding: TELEGRAM_CHAT_ID не задан — ключ user={} некому переслать",
                user_id,
            )
            return
        text = (
            f"Новый пользователь: `{user_id}` (chat\\_id `{chat_id}`)\n\n"
            f"Чтобы он поднимался после рестарта — добавь в Railway env:\n"
            f"`{token_env}={token}`\n\n"
            f"board\\_id `{board_id}` · space `{space_id}`\nbase\\_url: `{base_url}`"
        )
        try:
            await Notifier(chat_id=self._owner_chat_id).send(text)
        except Exception as exc:
            logger.error(
                "onboarding: не удалось переслать ключ владельцу — {}", exc
            )


# ── Тексты диалога ────────────────────────────────────────────────────────────

_WELCOME = (
    "Привет! Я AI-планировщик задач. Давай настроим тебя за пару минут.\n\n"
    "1. Зарегистрируйся в Kaiten (бесплатно): https://kaiten.ru/\n"
    "2. После регистрации открой Kaiten в браузере и пришли мне сюда "
    "*ссылку из адресной строки* "
    "(например `https://твой-аккаунт.kaiten.ru/...`)."
)

_TOKEN_PROMPT = (
    "Отлично! Теперь нужен API-ключ.\n\n"
    "Открой *Профиль → API-доступ* здесь: {origin}/profile\n"
    "Создай ключ и пришли его мне одним сообщением."
)

_DONE = (
    "Готово! Доска создана и настроена, ты подключён.\n\n"
    "Напиши *утро* — соберу план дня. Команды: /help"
)


# ── Построитель ConversationHandler ──────────────────────────────────────────

def build_onboarding_handler(cfg, service: OnboardingService) -> ConversationHandler:
    """Собирает ConversationHandler онбординга.

    Должен регистрироваться ПЕРВЫМ в Application — до главного conv_handler
    и CommandHandler('start'), чтобы перехватывать неизвестных пользователей.

    Известные пользователи (есть в cfg.users) фильтруются _UnknownUser и
    проваливаются к обычным хендлерам без изменений.
    """
    unknown = _UnknownUser(cfg)

    async def entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["ob"] = {}
        await update.message.reply_text(
            _WELCOME,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return OB_AWAIT_URL

    async def got_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        base_url, space_id = parse_kaiten_url(update.message.text)
        if not base_url:
            await update.message.reply_text(
                "Это не похоже на ссылку Kaiten. Пришли ссылку вида "
                "https://твой-аккаунт.kaiten.ru/…"
            )
            return OB_AWAIT_URL
        context.user_data["ob"].update(base_url=base_url, space_id=space_id)
        origin = base_url.replace("/api/latest", "")
        await update.message.reply_text(
            _TOKEN_PROMPT.format(origin=origin),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return OB_AWAIT_TOKEN

    async def got_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        token = (update.message.text or "").strip()
        ob = context.user_data["ob"]
        await update.message.reply_text("Проверяю ключ…")
        if not await service.validate_token(ob["base_url"], token, ob.get("space_id")):
            await update.message.reply_text(
                "Ключ не подошёл. Проверь и пришли ещё раз."
            )
            return OB_AWAIT_TOKEN
        ob["token"] = token
        if ob.get("space_id") is None:
            spaces = await service.list_spaces(ob["base_url"], token)
            if not spaces:
                await update.message.reply_text(
                    "Не нашёл ни одного пространства. "
                    "Создай пространство в Kaiten и пришли ключ снова."
                )
                return OB_AWAIT_TOKEN
            if len(spaces) == 1:
                ob["space_id"] = int(spaces[0]["id"])
            else:
                kb = [
                    [
                        InlineKeyboardButton(
                            s.get("title", f"#{s['id']}"),
                            callback_data=f"ob:space:{s['id']}",
                        )
                    ]
                    for s in spaces[:10]
                ]
                await update.message.reply_text(
                    "Выбери пространство для доски:",
                    reply_markup=InlineKeyboardMarkup(kb),
                )
                return OB_AWAIT_SPACE
        return await _finish(update, context)

    async def got_space(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data["ob"]["space_id"] = int(query.data.split(":")[2])
        await query.edit_message_text("Пространство выбрано.")
        return await _finish(update, context)

    async def _finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ob = context.user_data.get("ob", {})
        chat = update.effective_chat
        user = update.effective_user
        await context.bot.send_message(
            chat.id,
            "Создаю доску и настраиваю разделы… займёт до минуты.",
        )
        try:
            await service.provision_user(
                chat.id,
                user.username if user else None,
                ob["base_url"],
                ob["token"],
                ob["space_id"],
            )
        except Exception as exc:
            logger.exception("onboarding: провижининг упал — {}", exc)
            await context.bot.send_message(
                chat.id,
                "Не получилось завершить настройку. Попробуй /start заново.",
            )
            context.user_data.pop("ob", None)
            return ConversationHandler.END
        context.user_data.pop("ob", None)
        await context.bot.send_message(
            chat.id, _DONE, parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop("ob", None)
        await update.message.reply_text(
            "Онбординг отменён. /start — начать заново."
        )
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[
            CommandHandler("start", entry, filters=unknown),
            MessageHandler(filters.TEXT & ~filters.COMMAND & unknown, entry),
        ],
        states={
            OB_AWAIT_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_url),
            ],
            OB_AWAIT_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_token),
            ],
            OB_AWAIT_SPACE: [
                CallbackQueryHandler(got_space, pattern=r"^ob:space:\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", entry),
        ],
        name="onboarding",
        per_chat=True,
        per_user=True,
    )
