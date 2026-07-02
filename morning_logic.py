"""
morning_logic.py — утренняя логика.

[UPD 4] Полная перезапись для v4: алгоритм фаз 1–4.

Фаза 0 : резервация слотов для карточек уже в сегодняшней колонке
Фаза 1 : карточки с event_time.date() == today → фиксированные слоты
Фаза 2 : сортировка оставшихся по 9 группам приоритета
Фаза 3 : назначение event_time через update_card + перемещение в секцию
Фаза 4 : переполнение → следующий день / «Следующая неделя»

Понедельничная сборка карточек — без изменений (v3).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from loguru import logger

from kaiten_client import Card, KaitenClient, TAG_IDS
from board_logic import BoardLogic, WEEKDAY_COLUMNS

_TZ_MSK = timezone(timedelta(hours=3))

# Тег «вечерняя» (UPD 4, id=1097987)
_TAG_EVENING = 1097987

# Теги регулярных задач: если у карточки задано время События — она попадает в Phase 1
_RECURRING_FIXED_TAGS = frozenset([
    TAG_IDS["ежедневно"],
    TAG_IDS["еженедельно"],
    TAG_IDS["по будням"],
    TAG_IDS["по выходным"],
])

# Временные блоки (мин от начала суток)
# «Утро» и «День» — ярлыки приоритета, оба берут время из единого рабочего пула.
_WORK_START,    _WORK_END    = 9 * 60, 19 * 60   # 09:00–19:00 единый рабочий блок
_EVENING_START, _EVENING_END = 19 * 60, 22 * 60  # 19:00–22:00

# Длительность по умолчанию если size не задан (часы)
_DEFAULT_HOURS = 0.25

# Длительность size=999 если есть другие задачи в пуле (мин)
_SIZE_999_DEFAULT_MIN = 60


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _sorted_by_order(cards: list[Card]) -> list[Card]:
    return sorted(cards, key=lambda c: c.sort_order)


def _get_card_section(sorted_cards: list[Card], target: Card) -> str | None:
    """Определяет секцию карточки по её позиции в отсортированном списке."""
    current: str | None = None
    for card in sorted_cards:
        if card.blocked:
            current = card.block_reason
        elif card.id == target.id:
            return current
    return None


def _fmt_min(minutes: int) -> str:
    """Форматирует минуты от начала суток в строку HH:MM."""
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


# ── Планировщик одного блока ──────────────────────────────────────────────────

class _BlockScheduler:
    """Управляет временными слотами внутри одного блока.

    Принимает границы блока в минутах от начала суток.
    Поддерживает сегментированное размещение: задача может «обтекать»
    фиксированные события, занимая несколько свободных интервалов подряд.
    """

    def __init__(self, start_min: int, end_min: int) -> None:
        self._start = start_min
        self._end   = end_min
        self._occupied: list[tuple[int, int]] = []
        self._cursor: int = self._start

    def reserve(self, start_min: int, end_min: int) -> None:
        """Резервирует интервал (для карточек фазы 1 с фиксированным временем)."""
        end_min = min(end_min, self._end)
        if start_min < end_min:
            self._occupied.append((start_min, end_min))
            self._occupied.sort()

    def free_intervals_from_cursor(self) -> list[tuple[int, int]]:
        """Список свободных интервалов от текущего курсора до конца блока.

        Учитывает все зарезервированные и уже занятые интервалы.
        """
        pos = max(self._cursor, self._start)
        intervals: list[tuple[int, int]] = []
        for occ_s, occ_e in sorted(self._occupied):
            occ_s = max(occ_s, self._start)
            occ_e = min(occ_e, self._end)
            if occ_e <= pos:
                continue
            if occ_s > pos:
                intervals.append((pos, occ_s))
            pos = max(pos, occ_e)
        if pos < self._end:
            intervals.append((pos, self._end))
        return intervals

    def remaining_minutes(self) -> int:
        """Суммарное свободное время от курсора до конца блока."""
        return sum(e - s for s, e in self.free_intervals_from_cursor())

    def try_place_segmented(
        self,
        duration_min: int,
    ) -> list[tuple[int, int]] | None:
        """Размещает задачу по нескольким свободным интервалам (если нужно).

        Алгоритм «обтекания»: задача занимает столько свободного времени сколько нужно,
        перепрыгивая через фиксированные события. Если суммарного свободного
        времени недостаточно — возвращает None (задача уходит в overflow).

        Возвращает список сегментов [(start_min, end_min), ...] или None.

        Курсор продвигается до конца ПЕРВОГО сегмента (не последнего).
        Это позволяет следующей задаче занять слоты сразу после первого сегмента
        текущей задачи — не «перепрыгивать» через прерывистую задачу.
        """
        if duration_min <= 0:
            return None

        free = self.free_intervals_from_cursor()
        total_free = sum(e - s for s, e in free)

        if total_free < duration_min:
            return None  # не влезает даже по частям

        segments: list[tuple[int, int]] = []
        remaining = duration_min

        for seg_s, seg_e in free:
            if remaining <= 0:
                break
            chunk = min(remaining, seg_e - seg_s)
            seg_end = seg_s + chunk
            segments.append((seg_s, seg_end))
            self._occupied.append((seg_s, seg_end))
            remaining -= chunk

        self._occupied.sort()
        # Продвигаем курсор до конца ПЕРВОГО сегмента.
        # Следующая задача начнёт с этой точки и сможет занять свободное время
        # между сегментами текущей задачи (например, 09:30–12:00 если текущая
        # прыгнула через фиксированное событие с 12:00).
        if segments:
            self._cursor = segments[0][1]

        return segments


# ── MorningLogic ──────────────────────────────────────────────────────────────

class MorningLogic:
    """Утренняя логика переноса и расстановки карточек по времени (v4)."""

    def __init__(self, client: KaitenClient, logic: BoardLogic) -> None:
        self._client = client
        self._logic  = logic
        # Сегменты последнего запуска: {card_id: [("HH:MM", "HH:MM"), ...]}
        # Доступен после run() — используется scheduler.py для card_dict["segments"]
        self.last_segments: dict[int, list[tuple[str, str]]] = {}

    # ── Точка входа ───────────────────────────────────────────────────────────

    async def run(self, today: date) -> list[Card]:
        """Запускает утреннюю логику.

        Возвращает карточки сегодняшней колонки после всех перемещений.
        После вызова self.last_segments содержит сегменты для каждой карточки.
        Не бросает исключения: ошибки отдельных карточек логируются и пропускаются.
        """
        self.last_segments = {}  # сбрасываем перед каждым запуском
        if today.weekday() == 0:
            return await self._run_monday(today)
        return await self._run_regular(today)

    # ── Загрузка колонок ─────────────────────────────────────────────────────

    async def _load_week_cards(self) -> dict[int, list[Card]]:
        """Загружает пн–вс + «Следующая неделя» за один проход."""
        col_ids = [
            self._logic.column_ids["Понедельник"], self._logic.column_ids["Вторник"],
            self._logic.column_ids["Среда"],       self._logic.column_ids["Четверг"],
            self._logic.column_ids["Пятница"],     self._logic.column_ids["Суббота"],
            self._logic.column_ids["Воскресенье"], self._logic.column_ids["Следующая неделя"],
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

    # ── Перемещение карточки ─────────────────────────────────────────────────

    async def _move(
        self,
        card: Card,
        col_id: int,
        section: str,
        preloaded: dict[int, list[Card]],
    ) -> bool:
        """Перемещает карточку в col_id/section, синхронизирует preloaded."""
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
            old_col = card.column_id
            if old_col in preloaded:
                preloaded[old_col] = [c for c in preloaded[old_col] if c.id != card.id]
            card.column_id = col_id
            card.sort_order = sort_order
            preloaded.setdefault(col_id, []).append(card)
            return True
        except Exception as exc:
            logger.error(
                "move ERROR: «{}» (id={}) col={} sec={} — {}",
                card.title, card.id, col_id, section, exc,
            )
            return False

    # ── Назначение event_time ────────────────────────────────────────────────

    async def _set_event_time(self, card: Card, dt: datetime) -> bool:
        """Назначает event_time карточке через update_card (property id_590358)."""
        try:
            prop_value = {
                "date": dt.strftime("%Y-%m-%d"),
                "time": dt.strftime("%H:%M:%S"),
                "tzOffset": 180,
            }
            await self._client.update_card(
                card.id, properties={"id_590358": prop_value}
            )
            card.properties["id_590358"] = prop_value
            logger.info(
                "set_event_time: «{}» (id={}) → {}T{}",
                card.title, card.id, prop_value["date"], prop_value["time"],
            )
            return True
        except Exception as exc:
            logger.error(
                "set_event_time ERROR: «{}» (id={}) — {}",
                card.title, card.id, exc,
            )
            return False

    # ── Фазы 0–4: расписание для одного дня ─────────────────────────────────

    async def _schedule_today(
        self,
        candidates: list[Card],
        today: date,
        preloaded: dict[int, list[Card]],
    ) -> list[Card]:
        """Размещает кандидатов в сегодняшней колонке по алгоритму фаз 0–4.

        Заполняет self.last_segments: {card_id: [("HH:MM", "HH:MM"), ...]}.
        Возвращает свежие карточки сегодняшней колонки из API.
        """
        today_col_id  = self._logic.column_ids[WEEKDAY_COLUMNS[today.weekday()]]
        tomorrow      = today + timedelta(days=1)
        day_after     = today + timedelta(days=2)

        # Единый рабочий блок 09:00–19:00 (Утро и День — ярлыки приоритета, не окна)
        work_sched    = _BlockScheduler(_WORK_START,    _WORK_END)
        evening_sched = _BlockScheduler(_EVENING_START, _EVENING_END)

        # ── Фаза 0: резервируем слоты для карточек уже в сегодняшней колонке ──
        # Карточки уже в today_col_id не перемещаются (они на месте), но их временной
        # слот нужно зарезервировать чтобы фазы 1–3 не ставили задачи поверх них.
        for card in preloaded.get(today_col_id, []):
            if card.blocked or card.archived:
                continue
            et = card.event_time
            if et is None:
                continue
            et_local = et.astimezone(_TZ_MSK) if et.tzinfo else et.replace(tzinfo=_TZ_MSK)
            if et_local.date() != today:
                continue
            if (et_local.hour, et_local.minute) == (0, 0):
                continue
            start_min = et_local.hour * 60 + et_local.minute
            size_h = card.size if (card.size and card.size != 999) else _DEFAULT_HOURS
            end_min = start_min + round(size_h * 60)
            if et_local.hour < 19:
                work_sched.reserve(start_min, end_min)
            else:
                evening_sched.reserve(start_min, end_min)
            self.last_segments[card.id] = [(_fmt_min(start_min), _fmt_min(end_min))]
            logger.debug(
                "phase0 reserve: «{}» (id={}) {}–{}",
                card.title, card.id, _fmt_min(start_min), _fmt_min(end_min),
            )

        # ── Фаза 1: фиксированные события ─────────────────────────────────────
        # Попадают: event_time.date() == today
        # ИЛИ карточка с регулярным тегом (ежедневно/еженедельно/по будням/по выходным)
        # у которой явно задано время события (не 00:00).
        phase1:    list[Card] = []
        remaining: list[Card] = []

        for card in candidates:
            et = card.event_time
            if et is None:
                remaining.append(card)
                continue
            et_local = et.astimezone(_TZ_MSK) if et.tzinfo else et.replace(tzinfo=_TZ_MSK)
            is_today     = et_local.date() == today
            is_recurring = bool(set(card.tag_ids) & _RECURRING_FIXED_TAGS)
            has_time     = (et_local.hour, et_local.minute) != (0, 0)
            if is_today or (is_recurring and has_time):
                phase1.append(card)
            else:
                remaining.append(card)

        for card in sorted(phase1, key=lambda c: c.event_time):
            et = card.event_time
            et_local = et.astimezone(_TZ_MSK) if et.tzinfo else et.replace(tzinfo=_TZ_MSK)

            # Регулярная задача с другой датой: обновляем только дату на сегодня
            if et_local.date() != today:
                ev_dt = datetime(
                    today.year, today.month, today.day,
                    et_local.hour, et_local.minute, et_local.second,
                    tzinfo=_TZ_MSK,
                )
                await self._set_event_time(card, ev_dt)
                et_local = ev_dt

            hour      = et_local.hour
            start_min = hour * 60 + et_local.minute
            size_h    = card.size if (card.size and card.size != 999) else _DEFAULT_HOURS
            end_min   = start_min + round(size_h * 60)

            if hour < 19:
                # Секция на доске: до 12:00 → «Утро», с 12:00 → «День»
                section = "Утро" if hour < 12 else "День"
                work_sched.reserve(start_min, end_min)
            else:
                section = "Вечер"
                evening_sched.reserve(start_min, end_min)

            await self._move(card, today_col_id, section, preloaded)
            self.last_segments[card.id] = [(_fmt_min(start_min), _fmt_min(end_min))]

        logger.info(
            "_schedule_today: phase1={} remaining={}",
            len(phase1), len(remaining),
        )

        # ── Фаза 2: 9 групп приоритета ────────────────────────────────────────
        #
        # → Утро (не «вечерняя»):
        #   0. Критическое + deadline сегодня, size ASC
        #   1. Важное     + deadline сегодня, size ASC
        # → День (не «вечерняя»):
        #   2. Критическое + deadline завтра/послезавтра
        #   3. Важное      + deadline завтра/послезавтра
        #   4. Среднее     + deadline сегодня или завтра/послезавтра
        #   5. Все остальные без тега «вечерняя», size не 999
        #   (6 — см. ниже: size 999 вынесен в конец обработки)
        # → Вечер (тег «вечерняя»):
        #   6. Критическое + deadline сегодня, size ASC
        #   7. Важное      + deadline сегодня, size ASC
        #   8. Все остальные с тегом «вечерняя»
        #
        # Карточки size=999 без тега «вечерняя» собираются отдельно и
        # обрабатываются ПОСЛЕДНИМИ в рабочем блоке (после групп 0–5),
        # чтобы корректно вычислить сколько времени у них осталось.

        groups: list[list[Card]] = [[] for _ in range(9)]
        work_999: list[Card] = []   # size=999, не вечерняя — обрабатываются после групп 0–5

        # (sched, секция на доске)
        group_sched_section: list[tuple[_BlockScheduler, str]] = [
            (work_sched,    "Утро"),   # 0: критическое + dl сегодня
            (work_sched,    "Утро"),   # 1: важное      + dl сегодня
            (work_sched,    "День"),   # 2: критическое + dl скоро
            (work_sched,    "День"),   # 3: важное      + dl скоро
            (work_sched,    "День"),   # 4: среднее     + dl скоро
            (work_sched,    "День"),   # 5: все остальные (не «вечерняя», size не 999)
            (evening_sched, "Вечер"),  # 6: вечерняя + критическое + dl сегодня
            (evening_sched, "Вечер"),  # 7: вечерняя + важное      + dl сегодня
            (evening_sched, "Вечер"),  # 8: вечерняя — остальные
        ]

        for card in remaining:
            tags   = set(card.tag_ids)
            is_eve = _TAG_EVENING in tags
            imp    = card.importance   # None / "среднее" / "важное" / "критическое"

            dd_parsed = card.due_date_parsed
            dl_date   = dd_parsed.date() if dd_parsed else None
            today_dl  = (dl_date == today)               if dl_date else False
            soon_dl   = (dl_date in (tomorrow, day_after)) if dl_date else False

            if not is_eve:
                if card.size == 999:
                    # size=999 без тега «вечерняя» — откладываем на самый конец
                    # рабочего блока, чтобы занять только оставшееся время
                    work_999.append(card)
                elif imp == "критическое" and today_dl:  groups[0].append(card)
                elif imp == "важное"       and today_dl:  groups[1].append(card)
                elif imp == "критическое" and soon_dl:   groups[2].append(card)
                elif imp == "важное"       and soon_dl:   groups[3].append(card)
                elif imp == "среднее"      and soon_dl:   groups[4].append(card)
                else:                                      groups[5].append(card)
            else:
                if   imp == "критическое" and today_dl:  groups[6].append(card)
                elif imp == "важное"       and today_dl:  groups[7].append(card)
                else:                                      groups[8].append(card)

        # Группы 0, 1, 6, 7 — сортировка по size ASC (сначала быстрые)
        for i in (0, 1, 6, 7):
            groups[i].sort(key=lambda c: c.size if (c.size and c.size != 999) else 0)

        logger.debug(
            "_schedule_today: groups = {} work_999={}",
            [len(g) for g in groups], len(work_999),
        )

        # ── Фаза 3: сегментированное назначение времени и перемещение ────────
        overflow: list[Card] = []

        # Обрабатываем группы 0–8, затем work_999 отдельно в конце
        processing_order: list[tuple[list[Card], _BlockScheduler, str]] = []
        for g_idx, group_cards in enumerate(groups):
            sched, section = group_sched_section[g_idx]
            processing_order.append((group_cards, sched, section))
        # work_999 — последними в рабочем блоке
        processing_order.append((work_999, work_sched, "День"))

        # Для вычисления has_more при size=999: считаем сколько задач
        # ещё будет обработано в том же блоке (work_sched или evening_sched)
        # после текущей позиции в processing_order.
        def _count_remaining_in_sched(
            from_step: int,
            from_card_idx: int,
            target_sched: _BlockScheduler,
        ) -> int:
            """Количество карточек, которые будут обработаны в target_sched
            после текущей позиции (from_step, from_card_idx)."""
            count = 0
            for step_idx, (step_cards, step_sched, _) in enumerate(processing_order):
                if step_sched is not target_sched:
                    continue
                start_ci = from_card_idx + 1 if step_idx == from_step else 0
                count += len(step_cards) - start_ci
            return count

        for step_idx, (group_cards, sched, section) in enumerate(processing_order):
            for card_idx, card in enumerate(group_cards):
                # Вычисляем нужную длительность
                if card.size == 999:
                    # size=999: 1 час если в блоке ещё есть задачи,
                    # иначе занять всё оставшееся свободное время.
                    more_after = _count_remaining_in_sched(step_idx, card_idx, sched)
                    if more_after > 0:
                        dur_min = _SIZE_999_DEFAULT_MIN
                        logger.debug(
                            "size=999 «{}» (id={}): has_more={}, dur={}min",
                            card.title, card.id, more_after, dur_min,
                        )
                    else:
                        dur_min = sched.remaining_minutes()
                        logger.debug(
                            "size=999 «{}» (id={}): last in block, dur={}min",
                            card.title, card.id, dur_min,
                        )
                elif card.size is None:
                    dur_min = round(_DEFAULT_HOURS * 60)
                else:
                    dur_min = max(1, round(card.size * 60))

                if dur_min == 0:
                    logger.info(
                        "overflow (block full): «{}» (id={}) step={}",
                        card.title, card.id, step_idx,
                    )
                    overflow.append(card)
                    continue

                # Сегментированное размещение: задача огибает фиксированные события
                segments = sched.try_place_segmented(dur_min)
                if segments is None:
                    logger.info(
                        "overflow (no free time): «{}» (id={}) step={}",
                        card.title, card.id, step_idx,
                    )
                    overflow.append(card)
                    continue

                # Записываем сегменты в виде строк "HH:MM"
                str_segs = [(_fmt_min(s), _fmt_min(e)) for s, e in segments]
                self.last_segments[card.id] = str_segs
                logger.info(
                    "placed: «{}» (id={}) step={} segs={}",
                    card.title, card.id, step_idx,
                    " ".join(f"{s}–{e}" for s, e in str_segs),
                )

                # event_time = начало первого сегмента
                h, m  = divmod(segments[0][0], 60)
                ev_dt = datetime(today.year, today.month, today.day, h, m, tzinfo=_TZ_MSK)
                await self._set_event_time(card, ev_dt)
                await self._move(card, today_col_id, section, preloaded)

        # ── Фаза 4: переполнение ──────────────────────────────────────────────
        if overflow:
            logger.info("_schedule_today: overflow={}", len(overflow))
            await self._handle_overflow(overflow, today, preloaded)

        # Возвращаем свежие карточки из API (с установленными event_time)
        try:
            return await self._client.get_cards(today_col_id)
        except Exception as exc:
            logger.error("_schedule_today: не удалось загрузить итог — {}", exc)
            return []

    # ── Обработка переполнения ────────────────────────────────────────────────

    async def _handle_overflow(
        self,
        cards: list[Card],
        from_date: date,
        preloaded: dict[int, list[Card]],
    ) -> None:
        """Перемещает карточки в следующий день (без назначения event_time).

        После воскресенья (weekday=6) → «Следующая неделя».
        Карточки с тегом «вечерняя» попадают в секцию «Вечер», остальные — «Утро».
        Следующее утро само запланирует их по алгоритму фаз 0–4.
        """
        next_week_col = self._logic.column_ids["Следующая неделя"]
        next_day = from_date + timedelta(days=1)

        if next_day.weekday() == 0:
            # Следующий день был бы понедельником следующей недели
            target_col = next_week_col
        else:
            target_col = self._logic.column_ids[WEEKDAY_COLUMNS[next_day.weekday()]]

        for card in cards:
            try:
                section = "Вечер" if _TAG_EVENING in card.tag_ids else "Утро"
                so     = await self._logic.get_section_sort_order(target_col, section)
                result = await self._client.move_card(card.id, target_col, so)
                if result:
                    old_col = card.column_id
                    if old_col in preloaded:
                        preloaded[old_col] = [c for c in preloaded[old_col] if c.id != card.id]
                    card.column_id = target_col
                    card.sort_order = so
                    preloaded.setdefault(target_col, []).append(card)
                    logger.info(
                        "overflow → col={} sec={}: «{}» (id={})",
                        target_col, section, card.title, card.id,
                    )
                else:
                    logger.error("overflow FAIL: id={} «{}»", card.id, card.title)
            except Exception as exc:
                logger.error("overflow ERROR: id={} — {}", card.id, exc)

    # ── Обычный день (вт–вс) ─────────────────────────────────────────────────

    async def _run_regular(self, today: date) -> list[Card]:
        """Утренняя логика обычного дня (вт–вс).

        1. Загружает карточки вчерашней колонки.
        2. Маршрутизирует: еженедельно → следующая неделя,
           На контроле → сегодняшний «На контроле»,
           по будням на выходной / по выходным в будни → следующая неделя,
           остальные → кандидаты для фаз 0–4.
        3. Запускает _schedule_today.
        """
        logger.info("morning [regular]: {}", today.isoformat())

        preloaded = await self._load_week_cards()

        yesterday     = today - timedelta(days=1)
        yest_col_id   = self._logic.column_ids[WEEKDAY_COLUMNS[yesterday.weekday()]]
        today_col_id  = self._logic.column_ids[WEEKDAY_COLUMNS[today.weekday()]]
        next_week_col = self._logic.column_ids["Следующая неделя"]

        is_weekday = today.weekday() <= 4   # пн=0 … пт=4

        tag_weekly  = TAG_IDS["еженедельно"]
        tag_workday = TAG_IDS["по будням"]
        tag_weekend = TAG_IDS["по выходным"]

        yest_sorted = _sorted_by_order(preloaded.get(yest_col_id, []))
        tasks = [c for c in yest_sorted if not c.blocked and not c.archived]
        logger.info("morning [regular]: вчера col={} задач={}", yest_col_id, len(tasks))

        candidates: list[Card] = []

        for card in tasks:
            tags    = set(card.tag_ids)
            section = _get_card_section(yest_sorted, card)

            if tag_weekly in tags:
                # Еженедельно → следующая неделя (без time-scheduling)
                sec = BoardLogic.section_by_event_time(card)
                await self._move(card, next_week_col, sec, preloaded)

            elif section == "На контроле":
                # На контроле → та же секция сегодня (без time-scheduling)
                await self._move(card, today_col_id, "На контроле", preloaded)

            elif tag_workday in tags and not is_weekday:
                # По будням, но сегодня выходной → следующая неделя
                await self._move(card, next_week_col, "Утро", preloaded)

            elif tag_weekend in tags and is_weekday:
                # По выходным, но сегодня будний день → следующая неделя
                await self._move(card, next_week_col, "Утро", preloaded)

            else:
                # Всё остальное (ежедневно, по будням, по выходным, прочие) →
                # кандидаты для расписания
                candidates.append(card)

        logger.info("morning [regular]: кандидатов={}", len(candidates))
        return await self._schedule_today(candidates, today, preloaded)

    # ── Понедельник ───────────────────────────────────────────────────────────

    async def _run_monday(self, today: date) -> list[Card]:
        """Утренняя логика понедельника.

        Сборка карточек со всей недели — без изменений (v3).
        Размещение в понедельник — через фазы 0–4 (UPD 4).
        """
        logger.info("morning [monday]: {}", today.isoformat())

        monday_col     = self._logic.column_ids["Понедельник"]
        sunday_col     = self._logic.column_ids["Воскресенье"]
        saturday_col   = self._logic.column_ids["Суббота"]
        next_week_col  = self._logic.column_ids["Следующая неделя"]
        far_future_col = self._logic.column_ids["Далекие времена"]

        tag_weekly  = TAG_IDS["еженедельно"]
        tag_weekend = TAG_IDS["по выходным"]

        preloaded = await self._load_week_cards()

        # ── Шаг 1: «На контроле» воскресенья → понедельник ──────────────────
        sunday_sorted = _sorted_by_order(preloaded.get(sunday_col, []))
        control_sunday = [
            c for c in sunday_sorted
            if not c.blocked and not c.archived
            and _get_card_section(sunday_sorted, c) == "На контроле"
        ]
        logger.info("morning [monday]: На контроле воскресенья={}", len(control_sunday))
        for card in control_sunday:
            await self._move(card, monday_col, "На контроле", preloaded)

        # ── Шаг 2: Сборка пула (пн–вс + следующая неделя) ───────────────────
        week_col_ids = [
            self._logic.column_ids["Понедельник"], self._logic.column_ids["Вторник"],
            self._logic.column_ids["Среда"],       self._logic.column_ids["Четверг"],
            self._logic.column_ids["Пятница"],     self._logic.column_ids["Суббота"],
            self._logic.column_ids["Воскресенье"], next_week_col,
        ]

        pool: list[Card] = []
        for col_id in week_col_ids:
            col_sorted = _sorted_by_order(preloaded.get(col_id, []))
            for card in col_sorted:
                if card.blocked or card.archived:
                    continue
                if col_id == monday_col and _get_card_section(col_sorted, card) == "На контроле":
                    continue
                pool.append(card)

        logger.info("morning [monday]: пул={}", len(pool))

        # ── Шаг 0 (фикс): event_time на эту неделю → сразу в нужный день ────
        event_day_cards: list[Card] = []
        remaining_pool:  list[Card] = []

        for card in pool:
            et = card.event_time
            if et is not None:
                et_local   = et.astimezone(_TZ_MSK) if et.tzinfo else et.replace(tzinfo=_TZ_MSK)
                days_ahead = (et_local.date() - today).days
                if 0 <= days_ahead <= 6:
                    event_day_cards.append(card)
                    continue
            remaining_pool.append(card)

        logger.info(
            "morning [monday]: event_time на неделю={} остальных={}",
            len(event_day_cards), len(remaining_pool),
        )
        for card in event_day_cards:
            et_local   = card.event_time.astimezone(_TZ_MSK)
            target_col = self._logic.column_ids[WEEKDAY_COLUMNS[et_local.date().weekday()]]
            sec        = BoardLogic.section_by_event_time(card)
            await self._move(card, target_col, sec, preloaded)

        pool = remaining_pool

        # ── Батч-перенос пула во «Следующая неделя» ──────────────────────────
        BATCH_BASE = 10_000.0
        for i, card in enumerate(pool):
            try:
                batch_so = BATCH_BASE + i
                result   = await self._client.move_card(card.id, next_week_col, batch_so)
                if result is None:
                    logger.error("monday batch FAIL: id={} «{}»", card.id, card.title)
                    continue
                old_col = card.column_id
                if old_col in preloaded:
                    preloaded[old_col] = [c for c in preloaded[old_col] if c.id != card.id]
                card.column_id = next_week_col
                card.sort_order = batch_so
                preloaded.setdefault(next_week_col, []).append(card)
                logger.debug("monday batch OK: id={} «{}»", card.id, card.title)
            except Exception as exc:
                logger.error("monday batch ERROR: id={} — {}", card.id, exc)

        logger.info("morning [monday]: батч завершён, распределяем")

        # Маппинг «ПН»…«ВС» → column_id
        wd_to_col: dict[str, int] = {
            k: self._logic.column_ids[v]
            for k, v in zip(
                ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"],
                WEEKDAY_COLUMNS,
            )
        }

        # ── Классификация пула ────────────────────────────────────────────────
        monday_candidates: list[Card] = []

        for card in pool:
            tags = set(card.tag_ids)

            if tag_weekly in tags:
                # Еженедельно → колонка по weekday-полю
                wd     = card.weekday
                col_id = wd_to_col.get(wd, monday_col) if wd else monday_col
                sec    = BoardLogic.section_by_event_time(card)
                await self._move(card, col_id, sec, preloaded)

            elif tag_weekend in tags:
                # По выходным → суббота
                sec = BoardLogic.section_by_event_time(card)
                await self._move(card, saturday_col, sec, preloaded)

            else:
                # Ежедневно, по будням и все прочие → кандидаты для расписания пн
                monday_candidates.append(card)

        logger.info("morning [monday]: кандидатов для расписания={}", len(monday_candidates))

        # ── Фазы 0–4 для понедельника ─────────────────────────────────────────
        result = await self._schedule_today(monday_candidates, today, preloaded)

        # ── Далёкие времена → Следующая неделя ────────────────────────────────
        next_monday = today + timedelta(days=7)
        next_sunday = today + timedelta(days=13)

        try:
            far_cards = await self._client.get_cards(far_future_col)
        except Exception as exc:
            logger.error("morning [monday]: Далёкие времена недоступны — {}", exc)
            far_cards = []

        promoted = 0
        for card in far_cards:
            if card.blocked or card.archived:
                continue
            et = card.event_time
            if et is None:
                continue
            et_local = et.astimezone(_TZ_MSK) if et.tzinfo else et.replace(tzinfo=_TZ_MSK)
            et_date  = et_local.date()
            if next_monday <= et_date <= next_sunday:
                sec = BoardLogic.section_by_event_time(card)
                try:
                    so  = await self._logic.get_section_sort_order(next_week_col, sec)
                    res = await self._client.move_card(card.id, next_week_col, so)
                    if res:
                        promoted += 1
                        logger.info(
                            "monday: Далёкие → Следующая неделя «{}» (id={}) date={}",
                            card.title, card.id, et_date,
                        )
                    else:
                        logger.error("monday: Далёкие FAIL id={}", card.id)
                except Exception as exc:
                    logger.error("monday: Далёкие ERROR id={} — {}", card.id, exc)

        logger.info("morning [monday]: завершено, promoted_from_far={}", promoted)
        return result
