"""
scheduler.py — APScheduler джобы системы планирования.

Джобы:
    run_morning_job          — 06:30 ежедневно, план дня (итерирует по всем пользователям)
    run_evening_job          — 22:00 ежедневно, вечерний итог (снэпшот+diff+Claude)
    run_reminder_job         — каждую минуту, напоминания о событиях
    run_archive_cleanup_job  — каждый пн в 06:00, удаление старых карточек из Архива

Стек: APScheduler 3.x (AsyncIOScheduler), loguru, asyncio.
Флаги дублирования хранятся в SQLite через модуль db.
Мульти-пользователь: каждый пользователь описан UserSchedulerCtx.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

import db
from evening_logic import diff_day, CardLookupCtx
from kaiten_client import Card, KaitenClient, TAG_IDS
from board_logic import BoardLogic
from claude_client import ClaudeClient
from notifier import Notifier
from morning_logic import MorningLogic, _DEFAULT_HOURS
from user_config import UserConfig


# ── UserSchedulerCtx ──────────────────────────────────────────────────────────

@dataclass
class UserSchedulerCtx:
    """Контекст одного пользователя для джоб планировщика."""
    user_cfg: UserConfig
    morning:  MorningLogic
    notifier: Notifier
    kaiten:   KaitenClient
    logic:    BoardLogic


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
    segments: list | None = None,
) -> dict:
    """Конвертирует Card в dict для ClaudeClient.generate_morning_plan.

    Поля: id, title, description, importance, size, due_date, event_time,
          section, comments, segments.
    segments — список пар ("HH:MM", "HH:MM"); одна пара если задача не прерывается.
    """
    et = card.event_time
    return {
        "id":          card.id,
        "title":       card.title,
        "description": card.description,
        "importance":  card.importance,
        "size":        card.size,
        "due_date":    card.due_date,
        "event_time":  et.strftime("%H:%M") if et else None,
        "section":     section,
        "comments":    comments or [],
        "segments":    segments or [],
    }


def _extract_section_cards(
    cards: list[Card],
    comments_map: dict[int, list[str]] | None = None,
    segments_map: dict | None = None,
) -> list[dict]:
    """Разбирает список карточек колонки (с разделителями) в список dict с полем section.

    Проходит по sort_order, отслеживает текущую секцию через разделители.
    Разделители и архивированные в результат не включаются.
    Если переданы comments_map / segments_map — добавляет комментарии и сегменты.
    Результат сортируется по event_time (карточки без времени — в конце).
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
                segments=segments_map.get(card.id) if segments_map else None,
            ))

    result.sort(key=lambda d: d.get("event_time") or "99:99")
    return result


def _now_msk() -> datetime:
    """Текущее время в UTC+3."""
    return datetime.now(tz=_TZ_MSK)


def _compute_end_hhmm(d: dict) -> str | None:
    """Вычисляет строку HH:MM конца задачи по сегментам или event_time+size.

    Приоритеты:
      1. Непустые segments — берёт конец последнего сегмента.
      2. size задан и != 999 — event_time + size часов.
      3. size is None — event_time + _DEFAULT_HOURS (0.25 ч = 15 мин).
      4. size == 999 без segments — конец неизвестен, возвращает None.

    Вызывать только когда d["event_time"] гарантированно не None.
    """
    segments = d.get("segments") or []
    if segments:
        return segments[-1][1]
    size = d.get("size")
    if size == 999:
        return None
    et = d["event_time"]
    h, m = int(et[:2]), int(et[3:5])
    duration_h = size if size is not None else _DEFAULT_HOURS
    total = h * 60 + m + round(duration_h * 60)
    end_h, end_m = divmod(total, 60)
    return f"{end_h:02d}:{end_m:02d}"


# ── Ключ дедупликации напоминаний ─────────────────────────────────────────────

def _reminder_key(card_id: int, minutes_before: int) -> str:
    """Уникальный ключ для отправленного напоминания.

    Формат: "{card_id}:{minutes_before}"
    Сбрасывается при рестарте сервиса.
    """
    return f"{card_id}:{minutes_before}"


# ── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    """Оболочка над AsyncIOScheduler с джобами утра, вечера, напоминаний и архива.

    Поддерживает мульти-пользовательский режим: каждая джоба итерирует
    по всем пользователям из списка users.
    """

    def __init__(
        self,
        users: list[UserSchedulerCtx],
        claude: ClaudeClient,
    ) -> None:
        self._users  = users
        self._claude = claude

        # Ключи уже отправленных напоминаний, отдельный set на каждого пользователя.
        # Сбрасываются при рестарте сервиса.
        self._sent_reminders: dict[str, set[str]] = {
            u.user_cfg.user_id: set() for u in users
        }

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

        # Вечер — 22:00 по МСК
        self._scheduler.add_job(
            self._safe_evening,
            trigger=CronTrigger(hour=22, minute=0, timezone="Europe/Moscow"),
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

        # Очистка Архива — каждый понедельник в 06:00 (до утренней логики).
        # misfire_grace_time=21600 (6 ч): очистка не времязависима (в отличие от
        # утреннего плана), широкое окно снижает риск пропуска при простое Railway.
        self._scheduler.add_job(
            self._safe_archive_cleanup,
            trigger=CronTrigger(day_of_week="mon", hour=6, minute=0, timezone="Europe/Moscow"),
            id="archive_cleanup_job",
            name="Очистка Архива",
            replace_existing=True,
            misfire_grace_time=21600,
        )

        logger.info(
            "scheduler: джобы зарегистрированы "
            "(morning=06:30, evening=22:00, reminder=1m, archive_cleanup=пн 06:00)"
        )

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

    async def _safe_archive_cleanup(self) -> None:
        try:
            await self.run_archive_cleanup_job()
        except Exception as exc:
            logger.exception("scheduler: необработанная ошибка в archive_cleanup_job — {}", exc)

    # ── Утренняя джоба ────────────────────────────────────────────────────────

    async def run_morning_job(self) -> None:
        """Запускает утреннюю логику для каждого пользователя.

        Ошибка одного пользователя не прерывает остальных.
        """
        for user_ctx in self._users:
            try:
                await self._run_morning_for_user(user_ctx)
            except Exception as exc:
                logger.error(
                    "morning_job user={}: {}",
                    user_ctx.user_cfg.user_id, exc,
                )

    async def run_morning_for_user(self, user_ctx: UserSchedulerCtx) -> None:
        """Публичный метод для ручного запуска утренней логики (из handlers)."""
        await self._run_morning_for_user(user_ctx)

    async def run_replan_for_user(self, user_ctx: UserSchedulerCtx) -> str:
        """Пересобирает расписание пользователя с текущего момента и генерирует план.

        НЕ трогает флаг morning_done — пересборка разрешена сколько угодно раз за день.
        Не отправляет через notifier — возвращает готовый текст плана вызывающему коду
        (handlers.py использует _reply с этим текстом).
        """
        user_id = user_ctx.user_cfg.user_id
        now = _now_msk()
        loop = asyncio.get_event_loop()
        logger.info("replan user={}: старт ({})", user_id, now.isoformat())

        try:
            cards: list[Card] = await user_ctx.morning.replan(now)
            segments_map = user_ctx.morning.last_segments
        except Exception as exc:
            logger.error("replan user={}: ошибка morning.replan — {}", user_id, exc)
            return "Не удалось пересобрать план. Проверь логи."

        task_cards = [c for c in cards if not c.blocked and not c.archived]
        comments_list = await asyncio.gather(
            *[user_ctx.kaiten.get_comments(c.id) for c in task_cards],
            return_exceptions=True,
        )
        comments_map: dict[int, list[str]] = {
            c.id: (cmts if isinstance(cmts, list) else [])
            for c, cmts in zip(task_cards, comments_list)
        }

        cards_dicts = _extract_section_cards(
            cards,
            comments_map=comments_map,
            segments_map=segments_map,
        )

        # Фильтруем карточки с прошедшим временем: в отчёте «пересобрать» показываем
        # только слоты начиная с текущего момента. Карточки без event_time (например,
        # секция «На контроле») показываются всегда.
        # Задача, которая идёт СЕЙЧАС (начало в прошлом, конец в будущем), не удаляется —
        # её начало заменяется на текущий момент, чтобы показать оставшуюся часть.
        now_hhmm = now.strftime("%H:%M")
        filtered: list[dict] = []
        for d in cards_dicts:
            et = d.get("event_time")
            if et is None:
                # Нет времени (напр. «На контроле») — показываем всегда
                filtered.append(d)
                continue
            end_hhmm = _compute_end_hhmm(d)
            if end_hhmm is None:
                # size==999 без сегментов — конец неизвестен, старое поведение
                if et > now_hhmm:
                    filtered.append(d)
                continue
            if end_hhmm <= now_hhmm:
                # Задача полностью завершилась — пропускаем
                continue
            if et <= now_hhmm < end_hhmm:
                # Задача идёт сейчас: показываем оставшийся отрезок
                d["segments"] = [(now_hhmm, end_hhmm)]
                filtered.append(d)
            else:
                # et > now_hhmm — будущая задача, оставляем как есть
                filtered.append(d)
        cards_dicts = filtered

        date_str = _format_date_ru(now.date())
        try:
            plan_text = await self._claude.generate_morning_plan(cards_dicts, date_str)
        except Exception as exc:
            logger.error("replan user={}: ошибка generate_morning_plan — {}", user_id, exc)
            plan_text = (
                f"*{date_str}*\n\n"
                "Не удалось сгенерировать план. Посмотри доску вручную."
            )

        # Добавляем отчёт об overflow
        overflow = user_ctx.morning.last_overflow
        if overflow:
            lines = [f"\n\n📤 *Перенесено на другое время* ({len(overflow)}):"]
            for item in overflow:
                marker = "⚠️ " if item.get("risky") else ""
                lines.append(f"— {marker}{item['title']} → {item['target']}")
            plan_text += "\n".join(lines)

        # Логируем overflow-переносы в daily_events — только для пересборок (не для первого
        # утреннего запуска), чтобы вечерний итог видел перемещения, произошедшие в течение дня.
        for item in overflow:
            try:
                await loop.run_in_executor(
                    None, db.append_daily_event,
                    user_id, now.date(), "overflow", item["title"], item["target"],
                )
            except Exception as exc:
                logger.error("replan user={}: ошибка append_daily_event — {}", user_id, exc)

        logger.info(
            "replan user={}: завершено, карточек={} overflow={}",
            user_id, len(task_cards), len(overflow),
        )
        return plan_text

    async def _run_morning_for_user(self, user_ctx: UserSchedulerCtx) -> None:
        """Утренняя логика для одного пользователя: перенос карточек + план от Claude.

        Проверяет флаг в SQLite — если уже выполнено сегодня, пропускает.
        """
        today   = _now_msk().date()   # московское время, не UTC-сервер
        user_id = user_ctx.user_cfg.user_id
        logger.info("morning_job user={}: старт ({})", user_id, today.isoformat())

        # ── Проверка флага ────────────────────────────────────────────────────
        loop = asyncio.get_event_loop()
        already_done = await loop.run_in_executor(
            None, db.is_morning_done, today, user_id
        )
        if already_done:
            logger.info("morning_job user={}: уже выполнено сегодня, пропускаем", user_id)
            return

        # ── Перенос карточек ──────────────────────────────────────────────────
        try:
            cards: list[Card] = await user_ctx.morning.run(today)
            segments_map = user_ctx.morning.last_segments
            logger.info(
                "morning_job user={}: перенос завершён, карточек сегодня={}",
                user_id, len(cards),
            )
        except Exception as exc:
            logger.error("morning_job user={}: ошибка morning.run — {}", user_id, exc)
            await user_ctx.notifier.send("Ошибка при переносе карточек. Проверь логи.")
            return

        # ── Загружаем комментарии параллельно для всех не-разделителей ────────
        task_cards = [c for c in cards if not c.blocked and not c.archived]
        comments_list = await asyncio.gather(
            *[user_ctx.kaiten.get_comments(c.id) for c in task_cards],
            return_exceptions=True,
        )
        comments_map: dict[int, list[str]] = {
            c.id: (cmts if isinstance(cmts, list) else [])
            for c, cmts in zip(task_cards, comments_list)
        }
        logger.debug(
            "morning_job user={}: загружены комментарии для {} карточек",
            user_id, len(task_cards),
        )

        # ── Форматируем карточки в list[dict] с секциями, комментариями, сегментами
        cards_dicts = _extract_section_cards(
            cards,
            comments_map=comments_map,
            segments_map=segments_map,
        )
        logger.debug(
            "morning_job user={}: подготовлено карточек для Claude={}",
            user_id, len(cards_dicts),
        )

        # ── Сохраняем утренний снэпшот для вечернего итога ───────────────────
        snapshot_cards = [
            {
                "id":         d["id"],
                "title":      d["title"],
                "size":       d["size"],
                "importance": d["importance"],
                "section":    d["section"],
            }
            for d in cards_dicts
        ]
        try:
            await loop.run_in_executor(
                None, db.save_morning_snapshot, user_id, today, snapshot_cards
            )
        except Exception as exc:
            logger.error("morning_job user={}: ошибка save_morning_snapshot — {}", user_id, exc)

        # ── Генерируем план через Claude ──────────────────────────────────────
        date_str = _format_date_ru(today)
        try:
            plan_text = await self._claude.generate_morning_plan(cards_dicts, date_str)
        except Exception as exc:
            logger.error(
                "morning_job user={}: ошибка generate_morning_plan — {}",
                user_id, exc,
            )
            plan_text = (
                f"*{date_str}*\n\n"
                "Не удалось сгенерировать план. Посмотри доску вручную."
            )

        # ── Добавляем отчёт об overflow ───────────────────────────────────────
        overflow = user_ctx.morning.last_overflow
        if overflow:
            lines = [f"\n\n📤 *Перенесено на другое время* ({len(overflow)}):"]
            for item in overflow:
                marker = "⚠️ " if item.get("risky") else ""
                lines.append(f"— {marker}{item['title']} → {item['target']}")
            plan_text += "\n".join(lines)

        # ── Отправляем в Telegram ─────────────────────────────────────────────
        try:
            await user_ctx.notifier.send_and_pin(plan_text)
        except Exception as exc:
            logger.error("morning_job user={}: ошибка send_and_pin — {}", user_id, exc)

        try:
            await user_ctx.notifier.send_card_buttons(task_cards)
        except Exception as exc:
            logger.error("morning_job user={}: ошибка send_card_buttons — {}", user_id, exc)

        # ── Ставим флаг ───────────────────────────────────────────────────────
        await loop.run_in_executor(None, db.set_morning_done, today, user_id)
        logger.info("morning_job user={}: завершено, флаг установлен", user_id)

    # ── Вечерняя джоба ───────────────────────────────────────────────────────

    async def run_evening_job(self) -> None:
        """Запускает вечерний итог для каждого пользователя.

        Ошибка одного пользователя не прерывает остальных.
        """
        for user_ctx in self._users:
            try:
                await self._run_evening_for_user(user_ctx)
            except Exception as exc:
                logger.error(
                    "evening_job user={}: {}",
                    user_ctx.user_cfg.user_id, exc,
                )

    async def run_evening_for_user(self, user_ctx: UserSchedulerCtx) -> None:
        """Публичный метод для ручного запуска вечерней логики (из handlers, команда «вечер»)."""
        await self._run_evening_for_user(user_ctx)

    async def _run_evening_for_user(self, user_ctx: UserSchedulerCtx) -> None:
        """Вечерний итог для одного пользователя: снэпшот утра + лог событий + текущее
        состояние сегодняшней колонки → diff_day → Claude → отправка → очистка данных дня.

        Идемпотентность: guard через флаг evening_done в SQLite (как is_morning_done у утра) —
        если пользователь уже получил вечерний отчёт сегодня (вручную командой «вечер» до
        автозапуска в 22:00, или наоборот), повторный запуск пропускается. Данные дня (снэпшот
        + лог событий) удаляются ТОЛЬКО после успешной отправки — если отправка не удалась,
        данные остаются для следующей попытки.
        """
        today   = _now_msk().date()
        user_id = user_ctx.user_cfg.user_id
        loop    = asyncio.get_event_loop()
        logger.info("evening_job user={}: старт ({})", user_id, today.isoformat())

        already_done = await loop.run_in_executor(None, db.is_evening_done, today, user_id)
        if already_done:
            logger.info("evening_job user={}: уже выполнено сегодня, пропускаем", user_id)
            return

        snapshot = await loop.run_in_executor(None, db.load_morning_snapshot, user_id, today)
        events   = await loop.run_in_executor(None, db.load_daily_events, user_id, today)

        today_col_id = user_ctx.logic.get_today_column_id()
        try:
            cards = await user_ctx.kaiten.get_cards(today_col_id)
        except Exception as exc:
            logger.error("evening_job user={}: не удалось загрузить карточки — {}", user_id, exc)
            return

        current_cards = _extract_section_cards(cards)

        # ── Предзагрузка реального состояния «неопознанных» карточек ─────────
        # «Неопознанные» — карточки снэпшота, которые исчезли из сегодняшней
        # колонки без явного события moved/overflow в daily_events. Без этой
        # проверки они слепо засчитывались бы как done, хотя могли быть
        # перенесены вручную в Kaiten UI (бот про перенос не знает).
        card_ctx: CardLookupCtx | None = None
        if snapshot:
            _current_ids_eve: set[int] = {
                c["id"] for c in current_cards if c.get("id") is not None
            }
            _moved_titles_eve: set[str] = {
                e["card_title"]
                for e in events
                if e.get("event_type") in ("moved", "overflow")
            }
            _unrecognized_ids: list[int] = [
                s["id"]
                for s in snapshot
                if s.get("id") is not None
                and s["id"] not in _current_ids_eve
                and s.get("title", "") not in _moved_titles_eve
            ]

            if _unrecognized_ids:
                logger.info(
                    "evening_job user={}: предзагрузка {} «неопознанных» карточек",
                    user_id, len(_unrecognized_ids),
                )
                _gc_results = await asyncio.gather(
                    *[user_ctx.kaiten.get_card(cid) for cid in _unrecognized_ids],
                    return_exceptions=True,
                )
                _card_lookup: dict[int, Card | None] = {}
                for _cid, _res in zip(_unrecognized_ids, _gc_results):
                    if isinstance(_res, BaseException):
                        logger.error(
                            "evening_job user={}: ошибка get_card id={} — {}",
                            user_id, _cid, _res,
                        )
                        _card_lookup[_cid] = None   # fallback: treat as done
                    else:
                        _card_lookup[_cid] = _res   # Card | None

                _client = user_ctx.kaiten
                _logic  = user_ctx.logic
                _tag_weekly   = _client.tag_id("еженедельно")
                _tag_weekdays = _client.tag_id("по будням")
                _tag_weekends = _client.tag_id("по выходным")
                _tag_daily    = _client.tag_id("ежедневно")
                card_ctx = CardLookupCtx(
                    lookup=_card_lookup,
                    today_col_id=today_col_id,
                    archive_col_id=_logic.column_ids["Архив"],
                    snapshot_date=today,
                    regular_tag_ids={
                        t for t in [_tag_weekly, _tag_weekdays, _tag_weekends, _tag_daily]
                        if t is not None
                    },
                    weekly_tag_id=_tag_weekly,
                    weekdays_tag_id=_tag_weekdays,
                    weekends_tag_id=_tag_weekends,
                    col_id_by_name=_logic.column_ids,
                )
            else:
                logger.debug("evening_job user={}: неопознанных карточек нет", user_id)

        diff = diff_day(snapshot, events, current_cards, card_ctx)

        try:
            summary = await self._claude.generate_evening_summary(
                diff["done"], diff["undone"], diff["moved"], diff["added"],
            )
        except Exception as exc:
            logger.error("evening_job user={}: ошибка generate_evening_summary — {}", user_id, exc)
            summary = "Не удалось сформировать вечерний итог дня."

        try:
            await user_ctx.notifier.send(summary)
        except Exception as exc:
            logger.error("evening_job user={}: ошибка отправки — {}", user_id, exc)
            return  # НЕ чистим данные дня — следующая попытка должна их увидеть

        await loop.run_in_executor(None, db.clear_daily_data, user_id, today)
        await loop.run_in_executor(None, db.set_evening_done, today, user_id)
        logger.info("evening_job user={}: завершено, данные дня очищены", user_id)

    # ── Джоба очистки Архива ─────────────────────────────────────────────────

    async def run_archive_cleanup_job(self) -> None:
        """Каждый понедельник в 06:00: очистка Архива для каждого пользователя.

        Ошибка одного пользователя не прерывает остальных.
        """
        for user_ctx in self._users:
            try:
                await self._run_archive_for_user(user_ctx)
            except Exception as exc:
                logger.error(
                    "archive_job user={}: {}",
                    user_ctx.user_cfg.user_id, exc,
                )

    async def _run_archive_for_user(self, user_ctx: UserSchedulerCtx) -> None:
        """Удаляет из Архива карточки старше 30 дней для одного пользователя.

        Фильтрация по полю last_moved_at (с fallback на updated_at если
        last_moved_at отсутствует в ответе API — реализовано в Card.last_moved_at_parsed).
        Карточки без временной метки не трогаются.
        """
        user_id = user_ctx.user_cfg.user_id
        try:
            cutoff = datetime.now(_TZ_MSK) - timedelta(days=30)
            logger.info(
                "archive_cleanup user={}: порог удаления={}",
                user_id, cutoff.date().isoformat(),
            )

            archive_col_id = user_ctx.logic.column_ids["Архив"]
            cards = await user_ctx.kaiten.get_cards(archive_col_id)

            total = 0
            skipped_blocked = 0
            skipped_no_ts = 0
            skipped_too_new = 0
            deleted = 0

            for card in cards:
                total += 1
                if card.blocked:
                    skipped_blocked += 1
                    continue
                ts = card.last_moved_at_parsed
                if ts is None:
                    skipped_no_ts += 1
                    continue
                if ts >= cutoff:
                    skipped_too_new += 1
                    continue
                ok = await user_ctx.kaiten.delete_card(card.id)
                if ok:
                    deleted += 1
                    logger.info(
                        "archive_cleanup user={}: удалена «{}» (id={}) moved={}",
                        user_id, card.title, card.id,
                        ts.date().isoformat(),
                    )
                else:
                    logger.warning(
                        "archive_cleanup user={}: не удалось удалить id={}",
                        user_id, card.id,
                    )

            logger.info(
                "archive_cleanup user={}: всего={} удалено={} пропущено(blocked={}, no_ts={}, too_new={})",
                user_id, total, deleted, skipped_blocked, skipped_no_ts, skipped_too_new,
            )

        except Exception as exc:
            logger.error("archive_cleanup user={}: ошибка — {}", user_id, exc)

    # ── Джоба напоминаний ─────────────────────────────────────────────────────

    async def run_reminder_job(self) -> None:
        """Проверяет напоминания для каждого пользователя.

        Ошибка одного пользователя не прерывает остальных.
        """
        for user_ctx in self._users:
            try:
                await self._run_reminders_for_user(user_ctx)
            except Exception as exc:
                logger.error(
                    "reminder_job user={}: {}",
                    user_ctx.user_cfg.user_id, exc,
                )

    async def _run_reminders_for_user(self, user_ctx: UserSchedulerCtx) -> None:
        """Проверяет карточки с тегом «напомнить» и event_time для одного пользователя.

        Правила (из requirements):
          - среднее:              напоминание за 15 мин
          - важное / критическое: напоминания за 30 мин и за 15 мин (с пометкой ВАЖНО)

        Дедупликация через in-memory dict _sent_reminders[user_id].
        """
        user_id      = user_ctx.user_cfg.user_id
        sent         = self._sent_reminders[user_id]
        now          = _now_msk()

        # ── Загружаем карточки сегодняшней колонки ────────────────────────────
        today_col_id = user_ctx.logic.get_today_column_id()
        try:
            cards = await user_ctx.kaiten.get_cards(today_col_id)
        except Exception as exc:
            logger.error(
                "reminder_job user={}: не удалось загрузить карточки col={} — {}",
                user_id, today_col_id, exc,
            )
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

        logger.debug(
            "reminder_job user={}: карточек с напоминанием={}",
            user_id, len(remind_cards),
        )

        for card in remind_cards:
            et = card.event_time
            # event_time хранится в UTC+3 — now тоже в UTC+3, сравниваем напрямую
            # Убираем tzinfo для простого вычисления разницы в минутах
            et_naive  = et.replace(tzinfo=None) if et.tzinfo else et
            now_naive = now.replace(tzinfo=None)

            # Событие уже прошло или слишком далеко (> 60 мин) — не проверяем
            delta_minutes = (et_naive - now_naive).total_seconds() / 60
            if delta_minutes < 0 or delta_minutes > 60:
                continue

            importance  = card.importance
            is_important = importance in _IMPORTANT_IMPORTANCE

            # Определяем какие напоминания нужны для этой карточки
            # Окно проверки: [target - 0.5, target + 0.5) минут
            # то есть ±30 секунд от точного момента (джоба раз в минуту)
            targets: list[int] = [30, 15] if is_important else [15]

            for target_minutes in targets:
                in_window = abs(delta_minutes - target_minutes) < 0.5

                if not in_window:
                    continue

                key = _reminder_key(card.id, target_minutes)
                if key in sent:
                    logger.debug(
                        "reminder_job user={}: дубликат пропущен card_id={} min={}",
                        user_id, card.id, target_minutes,
                    )
                    continue

                # ── Отправляем напоминание ────────────────────────────────────
                try:
                    await user_ctx.notifier.send_reminder(
                        card_title=card.title,
                        minutes_left=target_minutes,
                        important=is_important,
                    )
                    sent.add(key)
                    logger.info(
                        "reminder_job user={}: отправлено «{}» (id={}) за {} мин (important={})",
                        user_id, card.title, card.id, target_minutes, is_important,
                    )
                except Exception as exc:
                    logger.error(
                        "reminder_job user={}: ошибка отправки card_id={} — {}",
                        user_id, card.id, exc,
                    )

        # ── Чистим устаревшие ключи при накоплении ────────────────────────────
        # При большом количестве регулярных задач set будет расти.
        # Простая эвристика: если набралось > 500 ключей, сбрасываем весь set.
        # В худшем случае пользователь получит одно лишнее напоминание при следующем
        # запуске — приемлемая цена за простоту без персистентного хранилища.
        if len(sent) > 500:
            logger.warning(
                "reminder_job user={}: _sent_reminders > 500 ключей, сбрасываем set",
                user_id,
            )
            sent.clear()
