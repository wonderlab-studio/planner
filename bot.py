"""
bot.py — точка входа системы личного планирования.

Поддержка нескольких пользователей: загружает конфиг из users.json
(или fallback на env-переменные), создаёт изолированный набор зависимостей
для каждого пользователя и запускает единый Scheduler + Telegram polling.

Порядок запуска:
    1. load_users() — список пользователей
    2. Для каждого пользователя: KaitenClient → setup_board → BoardLogic / MorningLogic / Notifier
    3. Создаём Scheduler (один, итерирует по всем пользователям)
    4. Для каждого пользователя: UserHandlerCtx с коллбэками morning/evening/replan
    5. Запускаем Scheduler и Telegram Application (polling)
    6. Держим event loop живым до сигнала остановки

Остановка:
    Ctrl+C / SIGTERM → блок finally закрывает всё в обратном порядке.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

import db  # noqa: F401 — инициализирует SQLite при импорте
from board_logic import BoardLogic
from board_setup import setup_board
from claude_client import ClaudeClient
from handlers import HandlersConfig, UserHandlerCtx, build_handlers
from kaiten_client import KaitenClient
from morning_logic import MorningLogic
from notifier import Notifier
from scheduler import Scheduler, UserSchedulerCtx
from user_config import load_users


# ── Логирование ───────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Настраивает loguru: stdout + ротируемый файл."""
    logger.remove()  # убираем дефолтный хендлер

    # Консоль — INFO и выше, человекочитаемый формат
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )

    # Файл — DEBUG и выше, ротация раз в сутки, хранение 7 дней
    logger.add(
        "logs/bot.log",
        level="DEBUG",
        rotation="00:00",
        retention="7 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}",
    )


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main() -> None:
    _setup_logging()
    logger.info("bot: ── ЗАПУСК СИСТЕМЫ ──────────────────────────────────────")

    users = load_users()
    claude = ClaudeClient()

    user_sched_ctxs: list[UserSchedulerCtx] = []
    users_handler: dict[int, UserHandlerCtx] = {}
    kaiten_clients: list[KaitenClient] = []  # для закрытия в finally

    for user in users:
        # 1. Создать KaitenClient (с per-user параметрами тегов/полей если заданы)
        client = KaitenClient(
            board_id=user.kaiten_board_id,
            lane_id=user.kaiten_lane_id,
            token=user.kaiten_token,
            base_url=user.kaiten_base_url,
            tag_ids=user.tag_ids,
            importance_options=user.importance_options,
            weekday_options=user.weekday_options,
            field_ids=user.field_ids,
        )
        kaiten_clients.append(client)

        # 2. Настроить доску (если column_ids не заданы явно в конфиге)
        if user.column_ids:
            column_ids = user.column_ids
            if user.kaiten_lane_id == 0:
                lanes = await client.get_lanes()
                if lanes:
                    user.kaiten_lane_id = lanes[0]["id"]
                    client._lane_id = user.kaiten_lane_id
            logger.info("bot: column_ids для user={} взяты из конфига, board_setup пропущен", user.user_id)
        else:
            try:
                column_ids = await setup_board(client, user)
            except Exception as exc:
                logger.error("setup_board failed for user={}: {}", user.user_id, exc)
                raise

        # 3. Создать зависимости
        logic = BoardLogic(client, column_ids)
        morning = MorningLogic(client, logic)
        notifier = Notifier(chat_id=user.telegram_chat_id)

        # 4. Собрать контексты
        sched_ctx = UserSchedulerCtx(
            user_cfg=user,
            morning=morning,
            notifier=notifier,
            kaiten=client,
            logic=logic,
        )
        user_sched_ctxs.append(sched_ctx)

    # 5. Создать Scheduler (до UserHandlerCtx — нужен для коллбэков)
    scheduler = Scheduler(users=user_sched_ctxs, claude=claude)

    # 6. Создать handler-контексты с коллбэками утра/вечера/пересобрать
    for sched_ctx in user_sched_ctxs:
        user = sched_ctx.user_cfg

        # Scheduler.run_morning_for_user() сам отправляет план через Notifier.
        # Handlers проверяют `if plan_text:` — None не дублирует отправку.
        async def morning_routine(_sc=sched_ctx) -> None:
            await scheduler.run_morning_for_user(_sc)

        async def evening_routine(_sc=sched_ctx) -> None:
            pass  # evening отключён в v4

        async def replan_routine(_sc=sched_ctx) -> str:
            return await scheduler.run_replan_for_user(_sc)

        users_handler[user.telegram_chat_id] = UserHandlerCtx(
            user_id=user.user_id,
            kaiten=sched_ctx.kaiten,
            logic=sched_ctx.logic,
            notifier=sched_ctx.notifier,
            morning_routine=morning_routine,
            evening_routine=evening_routine,
            replan_routine=replan_routine,
        )

    # 7. Telegram Application
    cfg = HandlersConfig(users=users_handler, claude=claude)
    app = build_handlers(cfg)

    # 8. Запуск
    stop_event = asyncio.Event()

    # Обработчик SIGTERM (для Railway / Docker graceful shutdown)
    def _on_sigterm(*_):
        logger.info("bot: получен SIGTERM, инициируем остановку")
        stop_event.set()

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (OSError, ValueError):
        # Windows или контекст без сигналов — просто пропускаем
        pass

    try:
        scheduler.start()
        logger.info("bot: APScheduler запущен")

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info(
            "bot: Telegram polling запущен — ожидаем сообщений ({} пользователей)",
            len(users),
        )

        # Держим event loop живым до Ctrl+C или SIGTERM
        await stop_event.wait()

    except (KeyboardInterrupt, SystemExit):
        logger.info("bot: получен сигнал остановки (KeyboardInterrupt/SystemExit)")

    finally:
        logger.info("bot: ── ОСТАНОВКА ────────────────────────────────────────")

        # Останавливаем polling и Application
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("bot: Telegram Application остановлен")
        except Exception as exc:
            logger.error("bot: ошибка при остановке Application — {}", exc)

        # Останавливаем планировщик
        try:
            scheduler.stop()
            logger.info("bot: APScheduler остановлен")
        except Exception as exc:
            logger.error("bot: ошибка при остановке Scheduler — {}", exc)

        # Закрываем HTTP-клиенты Kaiten (по одному на пользователя)
        for client in kaiten_clients:
            try:
                await client.close()
            except Exception as exc:
                logger.error("bot: ошибка при закрытии KaitenClient — {}", exc)
        if kaiten_clients:
            logger.info("bot: KaitenClient(s) закрыты ({})", len(kaiten_clients))

        # Закрываем Claude API клиент
        try:
            await claude.close()
            logger.info("bot: ClaudeClient закрыт")
        except Exception as exc:
            logger.error("bot: ошибка при закрытии ClaudeClient — {}", exc)

        logger.info("bot: ── СИСТЕМА ОСТАНОВЛЕНА ──────────────────────────────")


if __name__ == "__main__":
    asyncio.run(main())
