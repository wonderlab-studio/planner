"""
board_logic.py — бизнес-логика работы с канбан-доской Kaiten.

Зависит от KaitenClient. Реализует:
- навигацию по колонкам (сегодня / вчера / по имени)
- вычисление sort_order для вставки в секцию (Утро/День/Вечер/На контроле)
- сортировку карточек по приоритету
- определение регулярных задач и их применимости к конкретному дню
"""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

from loguru import logger

from kaiten_client import (
    Card,
    Column,
    KaitenClient,
    TAG_IDS,
    TZ_MSK,
    WEEKDAY_OPTIONS,
    IMPORTANCE_OPTIONS,
)

# ── Константы ─────────────────────────────────────────────────────────────────

# Индекс 0=Пн … 6=Вс — совпадает с datetime.weekday()
WEEKDAY_COLUMNS: list[str] = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
]

# Приоритет секций в порядке заполнения
SECTIONS_ORDER: list[str] = ["Утро", "День", "Вечер"]

# Лимиты размера (size) по секциям
SECTION_LIMITS: dict[str, int] = {
    "Утро":  3,
    "День":  5,
    "Вечер": 3,
}

# Приоритет важности для сортировки (меньше = выше приоритет)
IMPORTANCE_RANK: dict[str | None, int] = {
    "критическое": 0,
    "важное":      1,
    "среднее":     2,
    None:          3,
}

# Регулярные теги (модульная константа — оставляем для обратной совместимости)
REGULAR_TAG_IDS: set[int] = {
    TAG_IDS["ежедневно"],
    TAG_IDS["по будням"],
    TAG_IDS["по выходным"],
    TAG_IDS["еженедельно"],
}

# Небольшой epsilon для вставки в пустую секцию
_EPSILON = 0.001


# ── BoardLogic ────────────────────────────────────────────────────────────────

class BoardLogic:
    """Бизнес-логика поверх KaitenClient."""

    def __init__(self, client: KaitenClient, column_ids: dict[str, int]) -> None:
        self._client = client
        self._column_ids = column_ids
        self._column_name_by_id: dict[int, str] = {v: k for k, v in column_ids.items()}
        self._weekday_index_by_column: dict[int, int] = {
            column_ids[name]: i
            for i, name in enumerate(WEEKDAY_COLUMNS)
            if name in column_ids
        }
        # Инстанс-версия регулярных тегов — учитывает per-user tag_ids из KaitenClient
        self._regular_tag_ids: set[int] = (
            {self._client.tag_id(n) for n in ("ежедневно", "по будням", "по выходным", "еженедельно")}
            - {None}
        )

    @property
    def column_ids(self) -> dict[str, int]:
        return self._column_ids

    @property
    def column_name_by_id(self) -> dict[int, str]:
        return self._column_name_by_id

    # ── Навигация по колонкам ─────────────────────────────────────────────────

    def get_column_id(self, day: str) -> int:
        """'Понедельник' → ID колонки. Поддерживает все колонки из column_ids."""
        col_id = self._column_ids.get(day)
        if col_id is None:
            raise ValueError(f"Неизвестное название колонки: {day!r}")
        return col_id

    def get_today_column_id(self) -> int:
        """Возвращает column_id колонки текущего дня недели (0=Пн … 6=Вс).

        Использует московское время (UTC+3), чтобы избежать расхождения с UTC
        на Railway-сервере возле полуночи MSK.
        """
        day_name = WEEKDAY_COLUMNS[datetime.now(TZ_MSK).date().weekday()]
        return self._column_ids[day_name]

    def get_yesterday_column_id(self) -> int:
        """Возвращает column_id колонки вчерашнего дня (UTC+3)."""
        yesterday = datetime.now(TZ_MSK).date() - timedelta(days=1)
        day_name = WEEKDAY_COLUMNS[yesterday.weekday()]
        return self._column_ids[day_name]

    def get_next_weekday_column_id(self, from_column_id: int) -> int | None:
        """Возвращает column_id следующего дня после указанного.

        Если следующий день — за воскресеньем, возвращает None
        (вызывающий код должен использовать «Следующая неделя»).
        """
        idx = self._weekday_index_by_column.get(from_column_id)
        if idx is None:
            return None
        next_idx = idx + 1
        if next_idx >= len(WEEKDAY_COLUMNS):
            return None  # после воскресенья → следующая неделя
        return self._column_ids[WEEKDAY_COLUMNS[next_idx]]

    def resolve_column_for_date(self, target_date: date) -> int:
        """Возвращает column_id для конкретной даты.

        Если дата попадает в текущую неделю (по сегодняшний-и-до-воскресенья включительно
        от МСК-сегодня) — колонка соответствующего дня недели.
        Если на следующей неделе — «Следующая неделя».
        Иначе — «Далекие времена».
        Та же логика, что уже используется в morning_logic.py (Фаза 1б) для карточек
        с будущим event_time — здесь вынесена в переиспользуемый метод.
        """
        today = datetime.now(TZ_MSK).date()
        this_week_sun = today + timedelta(days=6 - today.weekday())
        if target_date <= this_week_sun:
            return self._column_ids[WEEKDAY_COLUMNS[target_date.weekday()]]
        elif target_date <= this_week_sun + timedelta(days=7):
            return self._column_ids["Следующая неделя"]
        else:
            return self._column_ids["Далекие времена"]

    # ── Позиционирование в секции ─────────────────────────────────────────────

    async def get_section_sort_order(self, column_id: int, section: str) -> float:
        """Возвращает sort_order для вставки ПЕРВОЙ позицией в секцию.

        Алгоритм:
        1. Загружает все карточки колонки (включая разделители).
        2. Находит разделитель с block_reason == section.
        3. Находит следующий разделитель (или конец списка).
        4. Если в секции нет задач — divider.sort_order + epsilon.
        5. Если есть — среднее между divider.sort_order и первой задачей.
        """
        cards = await self._client.get_cards(column_id)
        if not cards:
            logger.warning(
                "get_section_sort_order: колонка {} пуста, возвращаем 1.0", column_id
            )
            return 1.0

        # Сортируем по sort_order
        sorted_cards = sorted(cards, key=lambda c: c.sort_order)

        # Ищем индекс разделителя нужной секции
        divider_idx: int | None = None
        for i, card in enumerate(sorted_cards):
            if card.blocked and card.block_reason == section:
                divider_idx = i
                break

        if divider_idx is None:
            logger.warning(
                "get_section_sort_order: разделитель «{}» не найден в колонке {}",
                section, column_id,
            )
            # Fallback: вставляем в конец
            return sorted_cards[-1].sort_order + _EPSILON

        divider = sorted_cards[divider_idx]

        # Следующий разделитель (граница секции)
        next_divider_idx: int | None = None
        for i in range(divider_idx + 1, len(sorted_cards)):
            if sorted_cards[i].blocked:
                next_divider_idx = i
                break

        # Карточки-задачи внутри секции (не разделители)
        if next_divider_idx is not None:
            section_cards = [
                c for c in sorted_cards[divider_idx + 1 : next_divider_idx]
                if not c.blocked
            ]
        else:
            section_cards = [
                c for c in sorted_cards[divider_idx + 1 :]
                if not c.blocked
            ]

        if not section_cards:
            # Секция пуста — вставляем сразу после разделителя
            result = divider.sort_order + _EPSILON
        else:
            # Вставляем перед первой задачей секции
            first_task = section_cards[0]
            result = (divider.sort_order + first_task.sort_order) / 2.0

        logger.debug(
            "get_section_sort_order: колонка={} секция={} → sort_order={}",
            column_id, section, result,
        )
        return result

    async def get_section_size_sum(self, column_id: int, section: str) -> int:
        """Возвращает сумму size карточек-задач в указанной секции колонки.

        Используется для определения куда вставлять следующую задачу
        по алгоритму утреннего переноса.
        """
        cards = await self._client.get_cards(column_id)
        sorted_cards = sorted(cards, key=lambda c: c.sort_order)

        # Находим границы секции
        divider_idx: int | None = None
        for i, card in enumerate(sorted_cards):
            if card.blocked and card.block_reason == section:
                divider_idx = i
                break

        if divider_idx is None:
            return 0

        next_divider_idx: int | None = None
        for i in range(divider_idx + 1, len(sorted_cards)):
            if sorted_cards[i].blocked:
                next_divider_idx = i
                break

        if next_divider_idx is not None:
            section_cards = sorted_cards[divider_idx + 1 : next_divider_idx]
        else:
            section_cards = sorted_cards[divider_idx + 1 :]

        return sum(c.size or 0 for c in section_cards if not c.blocked)

    # ── Определение секции по времени события ────────────────────────────────

    @staticmethod
    def section_by_event_time(card: Card) -> str:
        """Определяет секцию (Утро/День/Вечер) по event_time карточки.

        00:00–05:59 → Утро (вне рабочего диапазона, дефолт)
        06:00–11:59 → Утро
        12:00–17:59 → День
        18:00–23:59 → Вечер
        Нет времени → Утро (по умолчанию)
        """
        et = card.event_time
        if et is None:
            return "Утро"
        hour = et.hour
        if hour < 12:
            return "Утро"
        if hour < 18:
            return "День"
        return "Вечер"

    # ── Сортировка по приоритету ──────────────────────────────────────────────

    def sort_cards_by_priority(self, cards: list[Card]) -> list[Card]:
        """Сортирует задачи для расстановки по дню.

        Разделители (blocked=True) исключаются.

        Порядок сортировки:
        1. Карточки с event_time — по времени события (по возрастанию).
        2. Дедлайн == сегодня, важность критическое/важное.
        3. Дедлайн == завтра, важность критическое/важное.
        4. Дедлайн == сегодня, важность среднее.
           Дедлайн == завтра, важность среднее.
        5. Остальные с дедлайном: сначала критические (по дате дедлайна),
           затем важные, затем обычные.
        6. Без дедлайна: сначала критические, потом важные, потом обычные.
        """
        today = datetime.now(TZ_MSK).date()
        tomorrow = today + timedelta(days=1)

        tasks = [c for c in cards if not c.blocked]

        def _due_date(card: Card) -> date | None:
            dt = card.due_date_parsed
            return dt.date() if dt else None

        def _sort_key(card: Card):
            dd = _due_date(card)
            imp = card.importance
            imp_rank = IMPORTANCE_RANK.get(imp, 3)
            et = card.event_time

            # Группа 0: есть event_time → сортируем по времени
            if et is not None:
                return (0, et.hour * 60 + et.minute, 0, 0, 0)

            # Группа 1: дедлайн сегодня, критическое/важное
            if dd == today and imp in ("критическое", "важное"):
                return (1, imp_rank, 0, 0, 0)

            # Группа 2: дедлайн завтра, критическое/важное
            if dd == tomorrow and imp in ("критическое", "важное"):
                return (2, imp_rank, 0, 0, 0)

            # Группа 3: дедлайн сегодня/завтра, среднее
            if dd == today and imp == "среднее":
                return (3, 0, 0, 0, 0)
            if dd == tomorrow and imp == "среднее":
                return (3, 1, 0, 0, 0)

            # Группа 4: остальные с дедлайном
            if dd is not None:
                days_left = (dd - today).days
                return (4, imp_rank, days_left, 0, 0)

            # Группа 5: без дедлайна
            size = card.size or 999
            return (5, imp_rank, 0, size, 0)

        return sorted(tasks, key=_sort_key)

    # ── Регулярные задачи ─────────────────────────────────────────────────────

    async def archive_card(self, card_id: int, comment: str | None = None) -> bool:
        """Архивирует карточку через KaitenClient.

        Делегирует в client.archive_card с ID архивной колонки из column_ids.
        Если передан comment — добавляет его перед перемещением.
        """
        result = await self._client.archive_card(
            card_id,
            self._column_ids["Архив"],
            comment,
        )
        if not result:
            logger.error("archive_card: не удалось архивировать карточку id={}", card_id)
            return False
        logger.info("archive_card: карточка id={} перемещена в Архив", card_id)
        return True

    def is_regular_task(self, card: Card) -> bool:
        """True если карточка имеет хотя бы один тег регулярности.

        Использует self._regular_tag_ids — per-instance множество, построенное
        из tag_id() клиента, чтобы корректно работать с per-user маппингами тегов.
        """
        return bool(set(card.tag_ids) & self._regular_tag_ids)

    def should_include_today(self, card: Card, today: date) -> bool:
        """Проверяет, должна ли регулярная задача попасть в текущий день.

        Логика:
        - ежедневно  → всегда True
        - по будням  → пн–пт (weekday 0–4)
        - по выходным → сб–вс (weekday 5–6)
        - еженедельно → только если card.weekday совпадает с сегодняшним днём
        """
        tag_ids = set(card.tag_ids)
        wd = today.weekday()  # 0=Пн … 6=Вс

        if TAG_IDS["ежедневно"] in tag_ids:
            return True

        if TAG_IDS["по будням"] in tag_ids and wd <= 4:
            return True

        if TAG_IDS["по выходным"] in tag_ids and wd >= 5:
            return True

        if TAG_IDS["еженедельно"] in tag_ids:
            card_wd = card.weekday  # 'ПН' / 'ВТ' / ...
            weekday_names = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
            return card_wd == weekday_names[wd]

        return False

    # ── Вспомогательные методы для утренней логики ───────────────────────────

    @staticmethod
    def _compute_section_sums(cards: list[Card]) -> dict[str, int]:
        """Принимает уже загруженные карточки одной колонки,
        возвращает сумму size задач по секциям.

        Пример результата: {"Утро": 4, "День": 2, "Вечер": 0, "На контроле": 1}

        Не делает никаких сетевых запросов — работает только с переданным списком.
        """
        sorted_cards = sorted(cards, key=lambda c: c.sort_order)

        # Разбиваем карточки по секциям через разделители
        current_section: str | None = None
        sums: dict[str, int] = {"Утро": 0, "День": 0, "Вечер": 0, "На контроле": 0}

        for card in sorted_cards:
            if card.blocked:
                current_section = card.block_reason  # переключаем секцию
                if current_section and current_section not in sums:
                    sums[current_section] = 0
            else:
                if current_section:
                    sums[current_section] = sums.get(current_section, 0) + (card.size or 0)

        return sums

    async def find_slot_for_card(
        self,
        card: Card,
        start_column_id: int,
        preloaded_cards: dict[int, list[Card]] | None = None,
    ) -> tuple[int, str] | None:
        """Находит первый подходящий слот (column_id, section) для карточки.

        Обходит секции Утро/День/Вечер начиная с start_column_id,
        затем следующий день, потом послеследующий и т.д.
        Если ничего не влезает до воскресенья — возвращает (Следующая неделя, Утро).

        Параметры:
            card             — карточка для размещения
            start_column_id  — колонка с которой начинаем поиск
            preloaded_cards  — словарь {column_id: [Card, ...]} с уже загруженными
                               карточками. Если передан — сетевые запросы не делаются.
                               Вызывающий код должен загрузить карточки всех колонок
                               заранее (один раз) и передать сюда.

        Возвращает (column_id, section) или None если не нашлось совсем.
        """
        # Собираем цепочку колонок: текущий день и все последующие дни недели
        col_chain: list[int] = []
        col_id: int | None = start_column_id
        while col_id is not None:
            col_chain.append(col_id)
            col_id = self.get_next_weekday_column_id(col_id)
        col_chain.append(self._column_ids["Следующая неделя"])

        for col_id in col_chain:
            is_next_week = (col_id == self._column_ids["Следующая неделя"])
            sections = SECTIONS_ORDER if not is_next_week else ["Утро"]

            # Получаем карточки колонки: из кеша или запросом
            if preloaded_cards is not None and col_id in preloaded_cards:
                col_cards = preloaded_cards[col_id]
                sums = self._compute_section_sums(col_cards)
            elif preloaded_cards is not None:
                # Колонка не предзагружена — считаем пустой (новая колонка / следующая неделя)
                sums = {"Утро": 0, "День": 0, "Вечер": 0, "На контроле": 0}
            else:
                # Режим без кеша: делаем запрос (допустимо для единичных вызовов)
                col_cards = await self._client.get_cards(col_id)
                sums = self._compute_section_sums(col_cards)

            for section in sections:
                current_sum = sums.get(section, 0)
                limit = SECTION_LIMITS.get(section, 999)
                if is_next_week or current_sum < limit:
                    logger.debug(
                        "find_slot_for_card: «{}» → column={} section={} (sum={}/{})",
                        card.title, col_id, section, current_sum, limit,
                    )
                    return (col_id, section)

        return None
