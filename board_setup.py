from __future__ import annotations

from loguru import logger

from kaiten_client import KaitenClient, KAITEN_SPACE_ID
from user_config import UserConfig, REQUIRED_COLUMN_NAMES

# Колонки дней (только в них создаются разделители)
_DAY_COLUMNS = {"Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"}

# Порядок разделителей внутри дневной колонки
_SECTIONS = ["Утро", "День", "Вечер", "На контроле"]

# Желаемый порядок колонок (sort_order задаётся позицией * 1000)
_COLUMN_ORDER = REQUIRED_COLUMN_NAMES

# Маппинг: нестандартное имя на доске → стандартное имя сервиса
# Используется если пользователь переименовал колонки или создал их с другими именами
_NAME_ALIASES: dict[str, str] = {
    "Пн":             "Понедельник",
    "Вт":             "Вторник",
    "Ср":             "Среда",
    "Чт":             "Четверг",
    "Пт":             "Пятница",
    "Сб":             "Суббота",
    "Вс":             "Воскресенье",
    "Далёкое будущее": "Далекие времена",
    "Далёкие времена": "Далекие времена",
}


async def setup_board(
    client: KaitenClient,
    user: UserConfig,
    *,
    needs_custom_fields: bool = True,
) -> tuple[dict[str, int], dict | None]:
    """
    Настраивает доску пользователя:
    - Создаёт недостающие колонки
    - Удаляет лишние пустые колонки
    - В дневных колонках создаёт разделители (если их нет)
    - Определяет lane_id (если user.kaiten_lane_id == 0)
    - Если needs_custom_fields=True — автоматически создаёт кастомные поля и теги
      в Kaiten-аккаунте и вызывает client.configure_custom_fields().

    Параметр needs_custom_fields определяется вызывающим кодом (bot.py) на основе
    того, есть ли уже сохранённая конфигурация поле для пользователя (field_ids в
    SQLite или users.json). Если конфигурация уже сохранена — False, поля не
    создаются повторно. Если конфигурация отсутствует — True (в том числе при
    частичном сбое предыдущей попытки, когда колонки уже существуют, но поля ещё
    нет). Это делает механизм устойчивым к частичным сбоям и не зависит от состояния
    колонок доски.

    Возвращает (column_ids, discovered_config):
        column_ids      — dict[str, int]: маппинг имя → id
        discovered_config — dict с ключами field_ids / importance_options / weekday_options /
                           time_of_day_options / tag_ids если needs_custom_fields=True
                           и создание завершилось успешно, иначе None.
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
                    title="-",
                    block_reason=section,
                    sort_order=float((idx + 1) * 100),
                )
                logger.info("board_setup: создан разделитель «{}» в колонке «{}»", section, col_name)

    # 6. Автосоздание кастомных полей и тегов (если запрошено вызывающим кодом)
    discovered_config: dict | None = None

    if needs_custom_fields:
        try:
            # Получаем список уже существующих кастомных полей (best-effort защита от дублей).
            # Если эндпоинт не поддерживается — get_custom_properties() вернёт [],
            # и защита от дублей просто не сработает (поля будут создаваться заново).
            existing_props = await client.get_custom_properties()
            existing_props_by_name = {
                p.get("name"): p.get("id")
                for p in existing_props
                if p.get("name") and p.get("id") is not None
            }
            if existing_props_by_name:
                logger.info(
                    "board_setup: найдено {} существующих кастомных полей: {}",
                    len(existing_props_by_name), list(existing_props_by_name.keys()),
                )

            # Событие (date)
            if "Событие" in existing_props_by_name:
                event_id = existing_props_by_name["Событие"]
                logger.info("board_setup: поле «Событие» уже существует, id={}", event_id)
            else:
                event_prop = await client.create_custom_property("Событие", "date")
                event_id = event_prop.get("id") if event_prop else None

            # Важность (select, одиночный)
            if "Важность" in existing_props_by_name:
                importance_id = existing_props_by_name["Важность"]
                logger.info("board_setup: поле «Важность» уже существует, id={}", importance_id)
            else:
                importance_prop = await client.create_custom_property(
                    "Важность", "select", multi_select=False
                )
                importance_id = importance_prop.get("id") if importance_prop else None
            importance_options: dict[str, int] = {}
            if importance_id is not None:
                for name in ("среднее", "важное", "критическое"):
                    val = await client.create_select_value(importance_id, name)
                    if val and val.get("id") is not None:
                        importance_options[name] = val["id"]

            # День недели (select, одиночный)
            if "День недели" in existing_props_by_name:
                weekday_id = existing_props_by_name["День недели"]
                logger.info("board_setup: поле «День недели» уже существует, id={}", weekday_id)
            else:
                weekday_prop = await client.create_custom_property(
                    "День недели", "select", multi_select=False
                )
                weekday_id = weekday_prop.get("id") if weekday_prop else None
            weekday_options: dict[str, int] = {}
            if weekday_id is not None:
                for name in ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"):
                    val = await client.create_select_value(weekday_id, name)
                    if val and val.get("id") is not None:
                        weekday_options[name] = val["id"]

            # Время дня (select, одиночный) — задел на будущее, не используется в логике планирования
            if "Время дня" in existing_props_by_name:
                tod_id = existing_props_by_name["Время дня"]
                logger.info("board_setup: поле «Время дня» уже существует, id={}", tod_id)
            else:
                tod_prop = await client.create_custom_property(
                    "Время дня", "select", multi_select=False
                )
                tod_id = tod_prop.get("id") if tod_prop else None
            time_of_day_options: dict[str, int] = {}
            if tod_id is not None:
                for name in ("Утро", "День", "Вечер"):
                    val = await client.create_select_value(tod_id, name)
                    if val and val.get("id") is not None:
                        time_of_day_options[name] = val["id"]

            field_ids: dict[str, str] = {}
            if event_id is not None:
                field_ids["event"] = f"id_{event_id}"
            if importance_id is not None:
                field_ids["importance"] = f"id_{importance_id}"
            if weekday_id is not None:
                field_ids["weekday"] = f"id_{weekday_id}"
            if tod_id is not None:
                field_ids["time_of_day"] = f"id_{tod_id}"

            # Теги: создаём через временную карточку в «Долгий ящик»,
            # затем получаем итоговые ID через GET /company/tags
            tag_ids: dict[str, int] = {}
            temp_card_id: int | None = None
            try:
                long_box_col = existing_by_name.get("Долгий ящик")
                if long_box_col:
                    temp = await client.create_card(
                        column_id=long_box_col, title="_setup_tags_tmp"
                    )
                    if temp:
                        temp_card_id = temp.id
                        tag_names = [
                            "ежедневно", "по будням", "по выходным", "еженедельно",
                            "напомнить", "вечерняя", "жёсткое событие", "не дробить", "рабочая",
                        ]
                        for tag_name in tag_names:
                            await client.add_tag_by_name(temp_card_id, tag_name)
                        all_tags = await client.get_tags()
                        tag_names_set = set(tag_names)
                        for t in all_tags:
                            if t.get("name") in tag_names_set and t.get("id") is not None:
                                tag_ids[t["name"]] = t["id"]
            finally:
                if temp_card_id is not None:
                    await client.delete_card(temp_card_id)

            discovered_config = {
                "field_ids":           field_ids,
                "importance_options":  importance_options,
                "weekday_options":     weekday_options,
                "time_of_day_options": time_of_day_options,
                "tag_ids":             tag_ids,
            }
            client.configure_custom_fields(**discovered_config)
            logger.info(
                "board_setup: автосоздание кастомных полей/тегов завершено для user={}: {}",
                user.user_id,
                {k: len(v) for k, v in discovered_config.items()},
            )
        except Exception as exc:
            logger.error(
                "board_setup: ошибка автосоздания кастомных полей/тегов user={} — {}",
                user.user_id, exc,
            )
            discovered_config = None

    # 7. Сформировать итоговый column_ids
    column_ids = {name: existing_by_name[name] for name in REQUIRED_COLUMN_NAMES if name in existing_by_name}
    user.column_ids = column_ids
    logger.info("board_setup: завершено для user={}, column_ids={}", user.user_id, list(column_ids.keys()))
    return column_ids, discovered_config
