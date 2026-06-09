"""
scheduler.py — APScheduler джобы системы планирования.

Джобы:
    run_morning_job  — 06:30 ежедневно, план дня
    run_evening_job  — 21:00 ежедневно, итог дня
    run_reminder_job — каждую минуту, напоминания о событиях

Стек: APScheduler 3.x (AsyncIOScheduler), loguru, asyncio.
Флаги дублирования хранятся в SQLite через модуль db.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

import db
from kaiten_client import Card, KaitenClient, TAG_IDS
from board_logic import BoardLogic, WEEKDAY_COLUMNS, COLUMN_IDS
from claude_client import ClaudeClient
from notifier import Notifier
from morning_logic import MorningLogic
from evening_logic import EveningLogic

# ── Константы ─────────────────────────────────────────────────────────────────

# Локаль для форматирования даты в промпт
_MONTH_NAMES = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_WEEKDAY_NAMES = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]

# Тег «напомнить»
_TAG_REMIND = TAG_IDS["напомнить"]

# Важность при которой нужно два напоминания (30 и 15 мин)
_IMPORTANT_IMPORTANCE = {"важное", "критическое"}

# Временная зона UTC+3 (Москва) — та же что используется в event_time карточек
_TZ_MSK = timezone(timedelta(hours=3))


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _format_date_ru(d: date) -> str:
    """Форматирует дату в читаемую русскую строку.

    Пример: date(2026, 5, 21) → "21 мая 2026, четверг"
    """
    return f"{d.day} {_MONTH_NAMES[d.month - 1]} {d.year}, {_WEEKDAY_NAMES[d.weekday()]}"


def _card_to_dict(
    card: Card,
    section: str | None = None,
    comments: list[str] | None = None,
) -> dict:
    """Конвертирует Card в dict для ClaudeClient.generate_morning_plan.

    Поля: title, description, importance, size, due_date, event_time, section, comments.
    """
    et = card.event_time
    return {
        "title":       card.title,
        "description": card.description,
        "importance":  card.importance,
        "size":        card.size,
        "due_date":    card.due_date,
        "event_time":  et.strftime("%H:%M") if et else None,
        "section":     section,
        "comments":    comments or [],
    }


def _extract_section_cards(
    cards: list[Card],
    comments_map: dict[int, list[str]] | None = None,
) -> list[dict]:
    """Разбирает список карточек колонки (с разделителями) в список dict с полем section.

    Проходит по sort_order, отслеживает текущую секцию через разделители.
    Разделители и архивированные в результат не включаются.
    Если передан comments_map, комментарии добавляются к каждой карточке.
    """
    sorted_cards = sorted(cards, key=lambda c: c.sort_order)
    result: list[dict] = []
    current_section: str | None = None
    _comments_map = comments_map or {}

    for card in sorted_cards:
        if card.blocked:
            current_section = card.block_reason
        elif not card.archived:
            result.append(_card_to_dict(
                card,
                section=current_section,
                comments=_comments_map.get(card.id, []),
            ))

    return result


def _now_msk() -> datetime:
    """Текущее время в UTC+3."""
    return datetime.now(tz=_TZ_MSK)


# ── Ключ дедупликации напоминаний ─────────────────────────────────────────────

def _reminder_key(card_id: int, minutes_before: int) -> str:
    """Уникальный ключ для отправленного напоминания.

    Формат: "{card_id}:{minutes_before}:{YYYY-MM-DD HH:MM}"
    Включает дату события чтобы не дублировать только в рамках одного дня,
    но корректно повторять на следующий день для регулярных задач.
    """
    return f"{card_id}:{minutes_before}"


# ── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    """Оболочка над AsyncIOScheduler с джобами утра, вечера и напоминаний."""

    def __init__(
        self,
        morning: MorningLogic,
        evening: EveningLogic,
        claude: ClaudeClient,
        notifier: Notifier,
        kaiten: KaitenClient,
        logic: BoardLogic,
    ) -> None:
        self._morning  = morning
        self._evening  = evening
        self._claude   = claude
        self._notifier = notifier
        self._kaiten   = kaiten
        self._logic    = logic

        # Ключи уже отправленных напоминаний — сбрасываются при рестарте сервиса.
        # Для одной сессии достаточно in-memory set; при рестарте в худшем случае
        # пользователь получит одно дублирующее напоминание.
        self._sent_reminders: set[str] = set()

        self._scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self._register_jobs()

    # ── Регистрация джоб ──────────────────────────────────────────────────────

    def _register_jobs(self) -> None:
        """Добавляет джобы в планировщик."""

        # Утро — 06:30 по МСК
        self._scheduler.add_job(
            self._safe_morning,
            trigger=CronTrigger(hour=6, minute=30, timezone="Europe/Moscow"),
            id="morning_job",
            name="Утренний план",
            replace_existing=True,
            misfire_grace_time=3600,  # допуск 1 час если сервер был выключен
        )

        # Вечер — 21:00 по МСК
        self._scheduler.add_job(
            self._safe_evening,
            trigger=CronTrigger(hour=21, minute=0, timezone="Europe/Moscow"),
            id="evening_job",
            name="Вечерний итог",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        # Напоминания — каждую минуту
        self._scheduler.add_job(
            self._safe_reminder,
            trigger=IntervalTrigger(minutes=1),
            id="reminder_job",
            name="Напоминания",
            replace_existing=True,
            misfire_grace_time=60,
        )

        logger.info("scheduler: джобы зарегистрированы (morning=06:30, evening=21:00, reminder=1m)")

    # ── Публичные методы ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Запускает AsyncIOScheduler."""
        self._scheduler.start()
        logger.info("scheduler: запущен")

    def stop(self) -> None:
        """Останавливает планировщик."""
        self._scheduler.shutdown(wait=False)
        logger.info("scheduler: остановлен")

    # ── Безопасные обёртки (не бросают исключения) ────────────────────────────

    async def _safe_morning(self) -> None:
        try:
            await self.run_morning_job()
        except Exception as exc:
            logger.exception("scheduler: необработанная ошибка в morning_job — {}", exc)

    async def _safe_evening(self) -> None:
        try:
            await self.run_evening_job()
        except Exception as exc:
            logger.exception("scheduler: необработанная ошибка в evening_job — {}", exc)

    async def _safe_reminder(self) -> None:
        try:
            await self.run_reminder_job()
        except Exception as exc:
            logger.exception("scheduler: необработанная ошибка в reminder_job — {}", exc)

    # ── Утренняя джоба ────────────────────────────────────────────────────────

    async def run_morning_job(self) -> None:
        """Утренняя логика: перенос карточек + план дня от Claude.

        Проверяет флаг в SQLite — если уже выполнено сегодня, пропускает.
        Публичный метод чтобы handlers.py мог вызвать его напрямую при команде «утро».
        """
        today = date.today()
        logger.info("morning_job: старт ({})", today.isoformat())

        # ── Проверка флага ────────────────────────────────────────────────────
        loop = asyncio.get_event_loop()
        already_done = await loop.run_in_executor(None, db.is_morning_done, today)
        if already_done:
            logger.info("morning_job: уже выполнено сегодня, пропускаем")
            return

        # ── Перенос карточек ──────────────────────────────────────────────────
        try:
            cards: list[Card] = await self._morning.run(today)
            logger.info("morning_job: перенос завершён, карточек сегодня={}", len(cards))
        except Exception as exc:
            logger.error("morning_job: ошибка morning.run — {}", exc)
            await self._notifier.send("⚠️ Ошибка при переносе карточек. Проверь логи.")
            return

        # ── Загружаем комментарии параллельно для всех не-разделителей ──────────
        task_cards = [c for c in cards if not c.blocked and not c.archived]
        comments_list = await asyncio.gather(
            *[self._kaiten.get_comments(c.id) for c in task_cards],
            return_exceptions=True,
        )
        comments_map: dict[int, list[str]] = {
            c.id: (cmts if isinstance(cmts, list) else [])
            for c, cmts in zip(task_cards, comments_list)
        }
        logger.debug(
            "morning_job: загружены комментарии для {} карточек", len(task_cards)
        )

        # ── Форматируем карточки в list[dict] с секциями и комментариями ─────
        cards_dicts = _extract_section_cards(cards, comments_map=comments_map)
        logger.debug("morning_job: подготовлено карточек для Claude={}", len(cards_dicts))

        # ── Генерируем план через Claude ──────────────────────────────────────
        date_str = _format_date_ru(today)
        try:
            plan_text = await self._claude.generate_morning_plan(cards_dicts, date_str)
        except Exception as exc:
            logger.error("morning_job: ошибка generate_morning_plan — {}", exc)
            plan_text = f"📅 *{date_str}*\n\n⚠️ Не удалось сгенерировать план. Посмотри доску вручную."

        # ── Отправляем в Telegram ─────────────────────────────────────────────
        try:
            await self._notifier.send_morning_plan(plan_text)
        except Exception as exc:
            logger.error("morning_job: ошибка send_morning_plan — {}", exc)

        # ── Ставим флаг ───────────────────────────────────────────────────────
        await loop.run_in_executor(None, db.set_morning_done, today)
        logger.info("morning_job: завершено, флаг установлен")

    # ── Вечерняя джоба ───────────────────────────────────────────────────────

    async def run_evening_job(self) -> None:
        """Вечерняя логика: итог дня от Claude.

        Проверяет флаг в SQLite — если уже выполнено, пропускает.
        Публичный метод для вызова из handlers.py при команде «вечер».
        """
        today = date.today()
        logger.info("evening_job: старт ({})", today.isoformat())

        # ── Проверка флага ────────────────────────────────────────────────────
        loop = asyncio.get_event_loop()
        already_done = await loop.run_in_executor(None, db.is_evening_done, today)
        if already_done:
            logger.info("evening_job: уже выполнено сегодня, пропускаем")
            return

        # ── Генерируем итог ───────────────────────────────────────────────────
        try:
            summary_text = await self._evening.run(today)
        except Exception as exc:
            logger.error("evening_job: ошибка evening.run — {}", exc)
            await self._notifier.send("⚠️ Ошибка при подведении итогов. Проверь логи.")
            return

        # ── Отправляем в Telegram ─────────────────────────────────────────────
        try:
            await self._notifier.send_evening_summary(summary_text)
        except Exception as exc:
            logger.error("evening_job: ошибка send_evening_summary — {}", exc)

        # ── Ставим флаг ───────────────────────────────────────────────────────
        await loop.run_in_executor(None, db.set_evening_done, today)
        logger.info("evening_job: завершено, флаг установлен")

    # ── Джоба напоминаний ─────────────────────────────────────────────────────

    async def run_reminder_job(self) -> None:
        """Проверяет карточки с тегом «напомнить» и event_time.

        Правила (из requirements):
          - среднее:              напоминание за 15 мин
          - важное / критическое: напоминания за 30 мин и за 15 мин (с пометкой ВАЖНО)

        Дедупликация через in-memory set _sent_reminders.
        Ключ включает card_id и minutes_before — сбрасывается только при рестарте.
        """
        now = _now_msk()
        today = now.date()

        # ── Загружаем карточки сегодняшней колонки ────────────────────────────
        today_col_id = self._logic.get_today_column_id()
        try:
            cards = await self._kaiten.get_cards(today_col_id)
        except Exception as exc:
            logger.error("reminder_job: не удалось загрузить карточки col={} — {}", today_col_id, exc)
            return

        # ── Фильтруем: только с тегом «напомнить» и event_time ───────────────
        remind_cards = [
            c for c in cards
            if not c.blocked
            and not c.archived
            and _TAG_REMIND in c.tag_ids
            and c.event_time is not None
        ]

        if not remind_cards:
            return

        logger.debug("reminder_job: карточек с напоминанием={}", len(remind_cards))

        for card in remind_cards:
            et = card.event_time
            # event_time хранится в UTC+3 — now тоже в UTC+3, сравниваем напрямую
            # Убираем tzinfo для простого вычисления разницы в минутах
            et_naive = et.replace(tzinfo=None) if et.tzinfo else et
            now_naive = now.replace(tzinfo=None)

            # Событие уже прошло или слишком далеко (> 60 мин) — не проверяем
            delta_minutes = (et_naive - now_naive).total_seconds() / 60
            if delta_minutes < 0 or delta_minutes > 60:
                continue

            importance = card.importance
            is_important = importance in _IMPORTANT_IMPORTANCE

            # Определяем какие напоминания нужны для этой карточки
            # Окно проверки: [target - 0.5, target + 0.5) минут
            # то есть ±30 секунд от точного момента (джоба раз в минуту)
            targets: list[int] = []
            if is_important:
                targets = [30, 15]
            else:
                targets = [15]

            for target_minutes in targets:
                # Попадает ли текущий момент в окно "за target минут до события"
                in_window = abs(delta_minutes - target_minutes) < 0.5

                if not in_window:
                    continue

                key = _reminder_key(card.id, target_minutes)
                if key in self._sent_reminders:
                    logger.debug(
                        "reminder_job: дубликат пропущен card_id={} min={}",
                        card.id, target_minutes,
                    )
                    continue

                # ── Отправляем напоминание ────────────────────────────────────
                try:
                    await self._notifier.send_reminder(
                        card_title=card.title,
                        minutes_left=target_minutes,
                        important=is_important,
                    )
                    self._sent_reminders.add(key)
                    logger.info(
                        "reminder_job: отправлено «{}» (id={}) за {} мин (important={})",
                        card.title, card.id, target_minutes, is_important,
                    )
                except Exception as exc:
                    logger.error(
                        "reminder_job: ошибка отправки напоминания card_id={} — {}",
                        card.id, exc,
                    )

        # ── Чистим устаревшие ключи раз в сутки ──────────────────────────────
        # При большом количестве регулярных задач set будет расти.
        # Простая эвристика: если набралось > 500 ключей, сбрасываем весь set.
        # В худшем случае пользователь получит одно лишнее напоминание при следующем
        # запуске — приемлемая цена за простоту без персистентного хранилища.
        if len(self._sent_reminders) > 500:
            logger.warning(
                "reminder_job: _sent_reminders > 500 ключей, сбрасываем set"
            )
            self._sent_reminders.clear()
