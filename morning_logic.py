"""
morning_logic.py — утренняя логика переноса карточек.

Класс MorningLogic реализует:
  - _run_regular : обычный день (вт–вс)
  - _run_monday  : понедельник (сброс недели + перераспределение)

Публичный метод:
    async def run(today: date) -> list[Card]
        Возвращает карточки сегодняшней колонки после всех перемещений.
        Используется вызывающим кодом для передачи в ClaudeClient.generate_morning_plan.
"""

from __future__ import annotations

from datetime import date, timedelta

from loguru import logger

from kaiten_client import Card, KaitenClient, TAG_IDS
from board_logic import (
    BoardLogic,
    COLUMN_IDS,
    WEEKDAY_COLUMNS,
)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _sorted_by_order(cards: list[Card]) -> list[Card]:
    """Сортирует список карточек по sort_order."""
    return sorted(cards, key=lambda c: c.sort_order)


def _get_card_section(sorted_cards: list[Card], target: Card) -> str | None:
    """Определяет секцию карточки внутри отсортированного списка.

    Проходит по карточкам в порядке sort_order, отслеживает текущий разделитель.
    Возвращает block_reason активного разделителя в момент встречи с target.
    """
    current_section: str | None = None
    for card in sorted_cards:
        if card.blocked:
            current_section = card.block_reason
        elif card.id == target.id:
            return current_section
    return None


def _cards_in_section(sorted_cards: list[Card], section: str) -> list[Card]:
    """Возвращает не-разделители из указанной секции отсортированного списка."""
    result: list[Card] = []
    in_target = False
    for card in sorted_cards:
        if card.blocked:
            in_target = (card.block_reason == section)
        elif in_target:
            result.append(card)
    return result


# ── MorningLogic ──────────────────────────────────────────────────────────────

class MorningLogic:
    """Утренняя логика переноса карточек с вчера на сегодня."""

    def __init__(self, client: KaitenClient, logic: BoardLogic) -> None:
        self._client = client
        self._logic = logic

    # ── Точка входа ───────────────────────────────────────────────────────────

    async def run(self, today: date) -> list[Card]:
        """Запускает утреннюю логику для заданной даты.

        Возвращает список карточек сегодняшней колонки после всех перемещений.
        Не бросает исключения — ошибки отдельных карточек логируются и пропускаются.
        """
        if today.weekday() == 0:
            return await self._run_monday(today)
        return await self._run_regular(today)

    # ── Загрузка всех колонок ─────────────────────────────────────────────────

    async def _load_week_cards(self) -> dict[int, list[Card]]:
        """Загружает карточки пн–вс + «Следующая неделя» за один проход.

        Возвращает словарь {column_id: [Card, ...]}.
        При ошибке загрузки отдельной колонки пишет WARNING и кладёт пустой список.
        """
        col_ids = [
            COLUMN_IDS["Понедельник"],
            COLUMN_IDS["Вторник"],
            COLUMN_IDS["Среда"],
            COLUMN_IDS["Четверг"],
            COLUMN_IDS["Пятница"],
            COLUMN_IDS["Суббота"],
            COLUMN_IDS["Воскресенье"],
            COLUMN_IDS["Следующая неделя"],
        ]
        preloaded: dict[int, list[Card]] = {}
        for col_id in col_ids:
            try:
                cards = await self._client.get_cards(col_id)
                preloaded[col_id] = cards
                logger.debug("preload: col={} cards={}", col_id, len(cards))
            except Exception as exc:
                logger.warning("preload: ошибка col={} — {}", col_id, exc)
                preloaded[col_id] = []
        return preloaded

    # ── Перемещение одной карточки ────────────────────────────────────────────

    async def _move(
        self,
        card: Card,
        col_id: int,
        section: str,
        preloaded: dict[int, list[Card]],
    ) -> bool:
        """Перемещает карточку в col_id/section, обновляет preloaded in-place.

        Не бросает исключений — при ошибке возвращает False.
        """
        try:
            sort_order = await self._logic.get_section_sort_order(col_id, section)
            result = await self._client.move_card(card.id, col_id, sort_order)
            if result is None:
                logger.error(
                    "move FAIL: «{}» (id={}) → col={} sec={}",
                    card.title, card.id, col_id, section,
                )
                return False

            logger.info(
                "move OK: «{}» (id={}) → col={} sec={} so={:.4f}",
                card.title, card.id, col_id, section, sort_order,
            )

            # Синхронизируем preloaded: убираем из старой колонки
            old_col = card.column_id
            if old_col in preloaded:
                preloaded[old_col] = [c for c in preloaded[old_col] if c.id != card.id]

            # Мутируем поля карточки и добавляем в новую колонку
            card.column_id = col_id
            card.sort_order = sort_order
            preloaded.setdefault(col_id, []).append(card)
            return True

        except Exception as exc:
            logger.error(
                "move ERROR: «{}» (id={}) → col={} sec={} — {}",
                card.title, card.id, col_id, section, exc,
            )
            return False

    # ── Обычный день (вт–вс) ─────────────────────────────────────────────────

    async def _run_regular(self, today: date) -> list[Card]:
        """Утренняя логика обычного дня: переносим вчера → сегодня."""
        logger.info("morning [regular]: {}", today.isoformat())

        # ── 0. Загрузка всех колонок ──────────────────────────────────────────
        preloaded = await self._load_week_cards()

        yesterday     = today - timedelta(days=1)
        yest_col_id   = COLUMN_IDS[WEEKDAY_COLUMNS[yesterday.weekday()]]
        today_col_id  = COLUMN_IDS[WEEKDAY_COLUMNS[today.weekday()]]
        next_week_col = COLUMN_IDS["Следующая неделя"]

        is_weekday = today.weekday() <= 4   # пн=0 … пт=4
        is_sunday  = today.weekday() == 6

        tag_weekly  = TAG_IDS["еженедельно"]
        tag_daily   = TAG_IDS["ежедневно"]
        tag_workday = TAG_IDS["по будням"]
        tag_weekend = TAG_IDS["по выходным"]

        # ── 1. Берём задачи из вчерашней колонки ─────────────────────────────
        yest_sorted = _sorted_by_order(preloaded.get(yest_col_id, []))
        tasks = [c for c in yest_sorted if not c.blocked and not c.archived]
        logger.info("morning [regular]: вчера col={} задач={}", yest_col_id, len(tasks))

        # ── 2. Классифицируем по строгому порядку требований ─────────────────
        group_weekly:  list[Card] = []
        group_control: list[Card] = []
        group_daily:   list[Card] = []
        group_workday: list[Card] = []
        group_weekend: list[Card] = []
        group_other:   list[Card] = []

        for card in tasks:
            tags = set(card.tag_ids)
            section = _get_card_section(yest_sorted, card)

            if tag_weekly in tags:
                # 1.1: еженедельно → следующая неделя
                group_weekly.append(card)
            elif section == "На контроле":
                # 1.2: секция «На контроле» → та же секция сегодня
                group_control.append(card)
            elif tag_daily in tags:
                # 1.3: ежедневно → секция по event_time
                group_daily.append(card)
            elif tag_workday in tags and is_weekday:
                # 1.4: по будням (только будни) → секция по event_time
                group_workday.append(card)
            elif tag_weekend in tags and is_sunday:
                # 1.5: по выходным (только вс) → секция по event_time
                group_weekend.append(card)
            else:
                # 1.6: остальные — по алгоритму приоритетов
                group_other.append(card)

        logger.debug(
            "morning [regular]: weekly={} control={} daily={} workday={} weekend={} other={}",
            len(group_weekly), len(group_control), len(group_daily),
            len(group_workday), len(group_weekend), len(group_other),
        )

        # ── 3. Перемещения в строгом порядке ─────────────────────────────────

        # 1.1 Еженедельно → Следующая неделя
        for card in group_weekly:
            sec = BoardLogic.section_by_event_time(card)
            await self._move(card, next_week_col, sec, preloaded)

        # 1.2 На контроле → На контроле сегодня
        for card in group_control:
            await self._move(card, today_col_id, "На контроле", preloaded)

        # 1.3 Ежедневно → секция по event_time
        for card in group_daily:
            sec = BoardLogic.section_by_event_time(card)
            await self._move(card, today_col_id, sec, preloaded)

        # 1.4 По будням → секция по event_time
        for card in group_workday:
            sec = BoardLogic.section_by_event_time(card)
            await self._move(card, today_col_id, sec, preloaded)

        # 1.5 По выходным → секция по event_time
        for card in group_weekend:
            sec = BoardLogic.section_by_event_time(card)
            await self._move(card, today_col_id, sec, preloaded)

        # 1.6 Остальные: сортируем по приоритету, ищем слот через find_slot_for_card
        sorted_other = self._logic.sort_cards_by_priority(group_other)
        for card in sorted_other:
            slot = await self._logic.find_slot_for_card(
                card, today_col_id, preloaded
            )
            if slot is None:
                # find_slot_for_card уже вернула (Следующая неделя, Утро) — не должно быть None,
                # но на случай неожиданного сбоя — страхуемся
                logger.warning(
                    "morning [regular]: нет слота для «{}» (id={}) → Следующая неделя/Утро",
                    card.title, card.id,
                )
                await self._move(card, next_week_col, "Утро", preloaded)
            else:
                await self._move(card, slot[0], slot[1], preloaded)

        # ── 4. Возвращаем актуальные карточки сегодня ─────────────────────────
        logger.info("morning [regular]: завершено, загружаем итог col={}", today_col_id)
        try:
            return await self._client.get_cards(today_col_id)
        except Exception as exc:
            logger.error("morning [regular]: не удалось загрузить итог — {}", exc)
            return []

    # ── Понедельник ───────────────────────────────────────────────────────────

    async def _run_monday(self, today: date) -> list[Card]:
        """Утренняя логика понедельника: перераспределение всей недели."""
        logger.info("morning [monday]: {}", today.isoformat())

        monday_col    = COLUMN_IDS["Понедельник"]
        sunday_col    = COLUMN_IDS["Воскресенье"]
        saturday_col  = COLUMN_IDS["Суббота"]
        next_week_col = COLUMN_IDS["Следующая неделя"]
        far_future_col = COLUMN_IDS["Далекие времена"]

        tag_weekly  = TAG_IDS["еженедельно"]
        tag_daily   = TAG_IDS["ежедневно"]
        tag_workday = TAG_IDS["по будням"]
        tag_weekend = TAG_IDS["по выходным"]

        # ── 0. Загрузка колонок недели ────────────────────────────────────────
        preloaded = await self._load_week_cards()

        # ── Шаг 1: На контроле воскресенья → На контроле понедельника ─────────
        sunday_sorted = _sorted_by_order(preloaded.get(sunday_col, []))
        control_sunday = [
            c for c in sunday_sorted
            if not c.blocked and not c.archived
            and _get_card_section(sunday_sorted, c) == "На контроле"
        ]
        logger.info("morning [monday]: На контроле воскресенья={}", len(control_sunday))
        for card in control_sunday:
            await self._move(card, monday_col, "На контроле", preloaded)

        # ── Шаг 2: Собираем пул — все задачи пн–вс + следующей недели
        #           кроме «На контроле» понедельника (только что перенесённых туда) ──
        week_col_ids = [
            COLUMN_IDS["Понедельник"],
            COLUMN_IDS["Вторник"],
            COLUMN_IDS["Среда"],
            COLUMN_IDS["Четверг"],
            COLUMN_IDS["Пятница"],
            COLUMN_IDS["Суббота"],
            COLUMN_IDS["Воскресенье"],
            next_week_col,
        ]

        pool: list[Card] = []
        for col_id in week_col_ids:
            col_sorted = _sorted_by_order(preloaded.get(col_id, []))
            for card in col_sorted:
                if card.blocked or card.archived:
                    continue
                # Исключаем «На контроле» понедельника
                if col_id == monday_col and _get_card_section(col_sorted, card) == "На контроле":
                    continue
                pool.append(card)

        logger.info("morning [monday]: пул для перераспределения={}", len(pool))

        # ── Шаг 2a: Батч-перенос пула во «Следующая неделя» ──────────────────
        # sort_order назначаем начиная с 10000 — выше любых реальных значений,
        # чтобы не перебивать уже существующие карточки следующей недели.
        BATCH_BASE = 10_000.0
        for i, card in enumerate(pool):
            try:
                batch_so = BATCH_BASE + i
                result = await self._client.move_card(card.id, next_week_col, batch_so)
                if result is None:
                    logger.error(
                        "morning [monday]: batch FAIL id={} «{}»", card.id, card.title
                    )
                    continue
                # Синхронизируем preloaded
                old_col = card.column_id
                if old_col in preloaded:
                    preloaded[old_col] = [c for c in preloaded[old_col] if c.id != card.id]
                card.column_id = next_week_col
                card.sort_order = batch_so
                preloaded.setdefault(next_week_col, []).append(card)
                logger.debug("morning [monday]: batch OK id={} «{}»", card.id, card.title)
            except Exception as exc:
                logger.error(
                    "morning [monday]: batch ERROR id={} «{}» — {}", card.id, card.title, exc
                )

        logger.info("morning [monday]: батч завершён, начинаем распределение")

        # Маппинг «ПН»/«ВТ»/… → column_id
        weekday_str_to_col: dict[str, int] = {
            wd_str: COLUMN_IDS[col_name]
            for wd_str, col_name in zip(
                ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"],
                WEEKDAY_COLUMNS,
            )
        }

        # ── Классифицируем пул ────────────────────────────────────────────────
        # Порядок: еженедельно → ежедневно+по будням → по выходным → остальные
        group_weekly:  list[Card] = []
        group_regular: list[Card] = []   # ежедневно + по будням → понедельник
        group_weekend: list[Card] = []   # по выходным → суббота
        group_other:   list[Card] = []

        for card in pool:
            tags = set(card.tag_ids)
            if tag_weekly in tags:
                group_weekly.append(card)
            elif tag_daily in tags or tag_workday in tags:
                group_regular.append(card)
            elif tag_weekend in tags:
                group_weekend.append(card)
            else:
                group_other.append(card)

        logger.debug(
            "morning [monday]: weekly={} regular={} weekend={} other={}",
            len(group_weekly), len(group_regular), len(group_weekend), len(group_other),
        )

        # ── 2.1: Еженедельно → колонка по weekday-полю карточки ──────────────
        for card in group_weekly:
            wd = card.weekday          # 'ПН' / 'ВТ' / …
            col_id = weekday_str_to_col.get(wd, monday_col) if wd else monday_col
            sec = BoardLogic.section_by_event_time(card)
            await self._move(card, col_id, sec, preloaded)

        # ── 2.2: Ежедневно + по будням → понедельник ─────────────────────────
        for card in group_regular:
            sec = BoardLogic.section_by_event_time(card)
            await self._move(card, monday_col, sec, preloaded)

        # ── 2.3: По выходным → суббота ────────────────────────────────────────
        for card in group_weekend:
            sec = BoardLogic.section_by_event_time(card)
            await self._move(card, saturday_col, sec, preloaded)

        # ── 2.4: Остальные → по приоритету начиная с понедельника ────────────
        sorted_other = self._logic.sort_cards_by_priority(group_other)
        for card in sorted_other:
            slot = await self._logic.find_slot_for_card(
                card, monday_col, preloaded
            )
            if slot is None:
                # find_slot_for_card возвращает (next_week, Утро) если нигде нет места,
                # None означает полный сбой — страхуемся
                logger.warning(
                    "morning [monday]: нет слота для «{}» (id={}) — оставляем в Следующей неделе",
                    card.title, card.id,
                )
                # Карточка уже в следующей неделе после батч-переноса — ничего не делаем
            else:
                await self._move(card, slot[0], slot[1], preloaded)

        # ── Шаг 5: Далёкие времена → Следующая неделя ────────────────────────
        # Карточки с event_time, попадающим на следующую неделю (пн–вс)
        next_monday = today + timedelta(days=7)
        next_sunday  = today + timedelta(days=13)

        try:
            far_cards = await self._client.get_cards(far_future_col)
        except Exception as exc:
            logger.error("morning [monday]: не удалось загрузить Далёкие времена — {}", exc)
            far_cards = []

        promoted = 0
        for card in far_cards:
            if card.blocked or card.archived:
                continue
            et = card.event_time
            if et is None:
                continue
            et_date = et.date()
            if next_monday <= et_date <= next_sunday:
                sec = BoardLogic.section_by_event_time(card)
                try:
                    so = await self._logic.get_section_sort_order(next_week_col, sec)
                    result = await self._client.move_card(card.id, next_week_col, so)
                    if result:
                        promoted += 1
                        logger.info(
                            "morning [monday]: Далёкие → Следующая неделя: «{}» (id={}) date={}",
                            card.title, card.id, et_date,
                        )
                    else:
                        logger.error(
                            "morning [monday]: не удалось перенести из Далёких id={}", card.id
                        )
                except Exception as exc:
                    logger.error(
                        "morning [monday]: ошибка переноса из Далёких id={} — {}", card.id, exc
                    )

        logger.info(
            "morning [monday]: завершено, promoted_from_far={}", promoted
        )

        # ── Возвращаем актуальные карточки понедельника ───────────────────────
        try:
            return await self._client.get_cards(monday_col)
        except Exception as exc:
            logger.error("morning [monday]: не удалось загрузить итог — {}", exc)
            return []
