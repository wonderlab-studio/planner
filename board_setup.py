from __future__ import annotations

from loguru import logger

from kaiten_client import KaitenClient, KAITEN_SPACE_ID
from user_config import UserConfig, REQUIRED_COLUMN_NAMES

# Колонки дней (только в них создаются разделители)
_DAY_COLUMNS = {"Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс", "Следующая неделя"}

# Порядок разделителей внутри дневной колонки
_SECTIONS = ["Утро", "День", "Вечер", "На контроле"]

# Желаемый порядок колонок (sort_order задаётся позицией * 1000)
_COLUMN_ORDER = REQUIRED_COLUMN_NAMES

# Маппинг: реальное имя на доске → стандартное имя сервиса
_NAME_ALIASES: dict[str, str] = {
    "Понедельник":     "Пн",
    "Вторник":         "Вт",
    "Среда":           "Ср",
    "Четверг":         "Чт",
    "Пятница":         "Пт",
    "Суббота":         "Сб",
    "Воскресенье":     "Вс",
    "Далекие времена": "Далёкое будущее",
    "Далёкие времена": "Далёкое будущее",
    "Далёкое будущее": "Далёкое будущее",  # на случай если уже стандартное
}


async def setup_board(client: KaitenClient, user: UserConfig) -> dict[str, int]:
    """
    Настраивает доску пользователя:
    - Создаёт недостающие колонки
    - Удаляет лишние пустые колонки
    - В дневных колонках создаёт разделители (если их нет)
    - Определяет lane_id (если user.kaiten_lane_id == 0)

    Возвращает column_ids: dict[str, int] — маппинг имя → id.
    Обновляет user.kaiten_lane_id и user.column_ids на месте.
    """
    logger.info("board_setup: начало для user={}, board={}", user.user_id, user.kaiten_board_id)

    # 1. Определить lane_id если не задан
    if user.kaiten_lane_id == 0:
        lanes = await client.get_lanes()
        if not lanes:
            raise RuntimeError(f"Доска {user.kaiten_board_id} не имеет lanes")
        user.kaiten_lane_id = lanes[0]["id"]
        client._lane_id = user.kaiten_lane_id
        logger.info("board_setup: lane_id определён автоматически: {}", user.kaiten_lane_id)

    # 2. Получить существующие колонки
    existing = await client.get_columns()   # list[Column] с атрибутами .id, .title
    existing_by_id: dict[int, str] = {col.id: col.title for col in existing}
    required_set = set(REQUIRED_COLUMN_NAMES)

    existing_by_name: dict[str, int] = {}
    for col in existing:
        if col.title in _NAME_ALIASES:
            # Реальное имя на доске → стандартное имя (alias всегда перезаписывает)
            standard = _NAME_ALIASES[col.title]
            existing_by_name[standard] = col.id
        elif col.title in required_set:
            # Стандартное имя — добавляем только если alias ещё не занял место
            if col.title not in existing_by_name:
                existing_by_name[col.title] = col.id

    # 2.5 Удалить пустые стандартно-названные колонки, если их место уже занято alias-маппингом
    for col in existing:
        if col.title not in required_set:
            continue  # нестандартное имя — обрабатывается в шаге 4
        mapped_id = existing_by_name.get(col.title)
        if mapped_id is not None and mapped_id != col.id:
            # Эта стандартная колонка — дубль (alias-маппинг уже занял её место)
            dup_cards = await client.get_cards(col.id)
            if not dup_cards:
                deleted = await client.delete_column(col.id)
                if deleted:
                    logger.info(
                        "board_setup: удалена дублирующая пустая колонка «{}» id={}", col.title, col.id
                    )
            else:
                logger.warning(
                    "board_setup: дублирующая колонка «{}» id={} не пуста — пропускаем удаление",
                    col.title, col.id,
                )

    # 3. Создать недостающие колонки
    for i, name in enumerate(_COLUMN_ORDER):
        if name not in existing_by_name:
            result = await client.create_column(name, sort_order=float((i + 1) * 1000))
            if result:
                existing_by_name[name] = result["id"]
                existing_by_id[result["id"]] = name
                logger.info("board_setup: создана колонка «{}» id={}", name, result["id"])
            else:
                logger.error("board_setup: не удалось создать колонку «{}»", name)

    # 4. Удалить лишние колонки (только пустые)
    for col in existing:
        if col.title in required_set or col.title in _NAME_ALIASES:
            continue  # стандартное имя или alias-источник — не трогаем
        cards = await client.get_cards(col.id)
        if cards:
            logger.warning(
                "board_setup: колонка «{}» (id={}) не входит в стандартный набор, но не пуста — пропускаем",
                col.title, col.id
            )
        else:
            deleted = await client.delete_column(col.id)
            if deleted:
                logger.info("board_setup: удалена лишняя колонка «{}» id={}", col.title, col.id)

    # 5. В дневных колонках создать разделители (если отсутствуют)
    for col_name in _DAY_COLUMNS:
        col_id = existing_by_name.get(col_name)
        if not col_id:
            continue
        cards = await client.get_cards(col_id)
        existing_sections = {c.block_reason for c in cards if c.blocked and c.block_reason}
        for idx, section in enumerate(_SECTIONS):
            if section not in existing_sections:
                await client.create_blocked_card(
                    column_id=col_id,
                    title=section,
                    block_reason=section,
                    sort_order=float((idx + 1) * 100),
                )
                logger.info("board_setup: создан разделитель «{}» в колонке «{}»", section, col_name)

    # 6. Сформировать итоговый column_ids
    column_ids = {name: existing_by_name[name] for name in REQUIRED_COLUMN_NAMES if name in existing_by_name}
    user.column_ids = column_ids
    logger.info("board_setup: завершено для user={}, column_ids={}", user.user_id, list(column_ids.keys()))
    return column_ids
