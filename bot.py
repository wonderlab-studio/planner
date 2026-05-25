"""
bot.py — точка входа системы личного планирования.

Собирает все зависимости, запускает APScheduler и Telegram polling
в одном asyncio event loop.

Порядок запуска:
    1. Создаём все клиенты и логику
    2. Создаём Scheduler (регистрирует джобы, но ещё не стартует)
    3. Определяем morning_routine / evening_routine для handlers —
       они делегируют в Scheduler, который сам отправляет через Notifier
    4. Запускаем Scheduler
    5. Запускаем Telegram Application (polling)
    6. Держим event loop живым до сигнала остановки

Остановка:
    Ctrl+C → KeyboardInterrupt → блок finally закрывает всё в обратном порядке.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

import db  # noqa: F401 — инициализирует SQLite при импорте
from board_logic import BoardLogic
from claude_client import ClaudeClient
from evening_logic import EveningLogic
from handlers import HandlersConfig, build_handlers
from kaiten_client import KaitenClient
from morning_logic import MorningLogic
from notifier import Notifier
from scheduler import Scheduler


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

    # ── 1. Зависимости ────────────────────────────────────────────────────────
    kaiten   = KaitenClient()
    logic    = BoardLogic(kaiten)
    claude   = ClaudeClient()
    notifier = Notifier()
    morning  = MorningLogic(kaiten, logic)
    evening  = EveningLogic(kaiten, logic, claude)

    # ── 2. Планировщик ────────────────────────────────────────────────────────
    scheduler = Scheduler(
        morning=morning,
        evening=evening,
        claude=claude,
        notifier=notifier,
        kaiten=kaiten,
        logic=logic,
    )

    # ── 3. Коллбэки для handlers ──────────────────────────────────────────────
    # Scheduler.run_morning_job() / run_evening_job() возвращают None —
    # они сами отправляют результат через Notifier.
    # Handlers ожидают str: пустая строка — сигнал «дубль не нужен».
    # Handlers проверяют `if plan_text:` перед reply — пустую строку не шлют.

    async def morning_routine() -> str:
        """Делегирует в Scheduler; план уже отправлен через Notifier."""
        await scheduler.run_morning_job()
        return ""

    async def evening_routine() -> str:
        """Делегирует в Scheduler; итог уже отправлен через Notifier."""
        await scheduler.run_evening_job()
        return ""

    # ── 4. Telegram Application ───────────────────────────────────────────────
    cfg = HandlersConfig(
        kaiten=kaiten,
        logic=logic,
        claude=claude,
        notifier=notifier,
        morning_routine=morning_routine,
        evening_routine=evening_routine,
    )
    app = build_handlers(cfg)

    # ── 5. Запуск ─────────────────────────────────────────────────────────────
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
        logger.info("bot: Telegram polling запущен — ожидаем сообщений")

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

        # Закрываем HTTP-клиент Kaiten
        try:
            await kaiten.close()
            logger.info("bot: KaitenClient закрыт")
        except Exception as exc:
            logger.error("bot: ошибка при закрытии KaitenClient — {}", exc)

        # Закрываем Claude API клиент
        try:
            await claude.close()
            logger.info("bot: ClaudeClient закрыт")
        except Exception as exc:
            logger.error("bot: ошибка при закрытии ClaudeClient — {}", exc)

        logger.info("bot: ── СИСТЕМА ОСТАНОВЛЕНА ──────────────────────────────")


if __name__ == "__main__":
    asyncio.run(main())
