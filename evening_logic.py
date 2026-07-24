"""
evening_logic.py — сравнение утреннего снэпшота с текущим состоянием дня.

Чистая функция без I/O: принимает снэпшот утра, лог событий дня и текущие карточки
сегодняшней колонки — возвращает 4 категории для вечернего итога (done/undone/moved/added).
Хранение снэпшота/лога — в db.py (SQLite). Оркестрация (загрузка из БД, Kaiten, Claude,
отправка в Telegram, очистка после отправки) — в scheduler.py.

Уточнённая классификация: карточки, пропавшие без явного события moved/overflow,
НЕ засчитываются сразу done. scheduler.py параллельно запрашивает get_card для каждой
и передаёт результат в CardLookupCtx. diff_day использует его для точной классификации:
  - Card is None (удалена) → done
  - column_id == archive → done
  - регулярная задача в ожидаемой следующей колонке → done
  - иначе → moved (перенесена вручную, без явной команды бота)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from loguru import logger

from kaiten_client import Card


# ── Константы ─────────────────────────────────────────────────────────────────

# Маппинг кратких имён дней (Card.weekday) в полные (название колонки)
_WEEKDAY_SHORT_TO_FULL: dict[str, str] = {
    "ПН": "Понедельник",
    "ВТ": "Вторник",
    "СР": "Среда",
    "ЧТ": "Четверг",
    "ПТ": "Пятница",
    "СБ": "Суббота",
    "ВС": "Воскресенье",
}

# Полные имена дней в порядке datetime.weekday() (0=Пн … 6=Вс)
_WEEKDAY_FULL_ORDERED: list[str] = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
]


# ── CardLookupCtx ─────────────────────────────────────────────────────────────

@dataclass
class CardLookupCtx:
    """Контекст для уточнённой классификации карточек, пропавших из сегодняшней колонки.

    Формируется в scheduler.py ПЕРЕД вызовом diff_day:
    - lookup заполняется параллельными get_card-запросами для «неопознанных» карточек
      (те, что исчезли из колонки без явного события moved/overflow в daily_events)
    - остальные поля берутся из BoardLogic / KaitenClient пользователя

    Если список неопознанных пуст — card_ctx=None передаётся в diff_day
    (ветка уточнённой классификации просто не вызывается).
    """
    lookup: dict[int, Card | None]     # card_id → реальный Card или None (удалена/недоступна)
    today_col_id: int                   # ID сегодняшней колонки
    archive_col_id: int                 # ID колонки «Архив»
    snapshot_date: date                 # дата снэпшота утра (сегодня с т.з. планировщика)
    regular_tag_ids: set[int]           # теги регулярных задач (ежедневно/по будням/etc.)
    weekly_tag_id: int | None           # ID тега «еженедельно» или None если не задан
    weekdays_tag_id: int | None         # ID тега «по будням» или None
    weekends_tag_id: int | None         # ID тега «по выходным» или None
    col_id_by_name: dict[str, int]      # полное имя колонки → ID


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _expected_next_cycle_col_id(card: Card, ctx: CardLookupCtx) -> int | None:
    """Вычисляет ожидаемую колонку следующего цикла для регулярной задачи.

    Алгоритм согласован с overflow Фазы 4 morning_logic и правилами сбора
    карточек регулярных задач:

      еженедельно                               → колонка по card.weekday
      по выходным + снэпшот в будний (0–4)      → «Суббота»
      по будням   + снэпшот в выходной (5–6)    → «Следующая неделя»
      иначе (ежедневно / тип дня не меняется)   → колонка завтра
                                                   (воскресенье → «Следующая неделя»)

    Возвращает column_id или None если не удалось определить однозначно.
    """
    tag_ids = set(card.tag_ids)
    wd = ctx.snapshot_date.weekday()    # 0=Пн … 6=Вс

    # Еженедельно → колонка по дню недели карточки
    if ctx.weekly_tag_id is not None and ctx.weekly_tag_id in tag_ids:
        card_wd = card.weekday          # 'ПН', 'ВТ', …
        if card_wd:
            full_name = _WEEKDAY_SHORT_TO_FULL.get(card_wd)
            if full_name:
                return ctx.col_id_by_name.get(full_name)
        return None     # нет поля weekday → не можем определить

    # По выходным + сегодня будний → следующая суббота
    if ctx.weekends_tag_id is not None and ctx.weekends_tag_id in tag_ids and wd <= 4:
        return ctx.col_id_by_name.get("Суббота")

    # По будням + сегодня выходной → следующая неделя (понедельник)
    if ctx.weekdays_tag_id is not None and ctx.weekdays_tag_id in tag_ids and wd >= 5:
        return ctx.col_id_by_name.get("Следующая неделя")

    # Ежедневно / по будням на будний / по выходным на выходной → завтра
    if wd == 6:    # воскресенье — следующая неделя
        return ctx.col_id_by_name.get("Следующая неделя")
    tomorrow_name = _WEEKDAY_FULL_ORDERED[wd + 1]
    return ctx.col_id_by_name.get(tomorrow_name)


def _classify_disappeared(cid: int, ctx: CardLookupCtx) -> tuple[str, str | None]:
    """Классифицирует карточку, пропавшую из сегодняшней колонки без явного события.

    Использует ctx.lookup (предзагруженный get_card результат) для точной классификации.

    Возвращает (category, detail):
      - ('done',   None)                                  — выполнена / в архиве / удалена
      - ('moved',  'перенесено (обнаружено при сверке)')  — перенесена вручную в UI
      - ('undone', None)                                  — крайний случай: та же колонка
    """
    actual = ctx.lookup.get(cid)

    # Карточка удалена / API вернул None → безопасный дефолт: done
    if actual is None:
        return "done", None

    col = actual.column_id

    # Крайний случай: карточка в сегодняшней колонке (не должно возникать при нормальном
    # потоке — раз её нет в current_cards, но column_id совпадает → undone-дефолт)
    if col == ctx.today_col_id:
        return "undone", None

    # Колонка «Архив» → выполнена
    if col == ctx.archive_col_id:
        return "done", None

    # Регулярная задача → проверяем совпадение с ожидаемой следующей колонкой
    if ctx.regular_tag_ids and bool(set(actual.tag_ids) & ctx.regular_tag_ids):
        expected = _expected_next_cycle_col_id(actual, ctx)
        if expected is None:
            # Не удалось определить следующую колонку — считаем done (безопасный дефолт)
            return "done", None
        if col == expected:
            # Карточка ровно там, где ожидалась → штатный цикл регулярной задачи
            return "done", None

    # Карточка в произвольной другой колонке → перенесена вручную в Kaiten UI
    return "moved", "перенесено (обнаружено при сверке)"


# ── Основная функция ──────────────────────────────────────────────────────────

def diff_day(
    snapshot: list[dict] | None,
    events: list[dict],
    current_cards: list[dict],
    card_ctx: CardLookupCtx | None = None,
) -> dict:
    """Сравнивает утренний снэпшот с текущим состоянием карточек сегодняшней колонки.

    snapshot       — из db.load_morning_snapshot: [{"id","title","size","importance","section"}, ...]
                     или None (если утренняя логика сегодня не запускалась)
    events         — из db.load_daily_events: [{"event_type","card_title","detail","created_at"}, ...]
    current_cards  — текущие карточки сегодняшней колонки: [{"id","title","size","importance",
                     "section",...}, ...] (лишние ключи в dict допустимы и игнорируются)
    card_ctx       — контекст предзагруженных карточек для уточнённой классификации.
                     Если None — карточки без явного события засчитываются done (старое поведение).
                     Формируется в scheduler.py (параллельные get_card для «неопознанных» карточек).

    Алгоритм (сопоставление по id между snapshot и current_cards; по card_title между
    snapshot и events):
      - undone: карточки снэпшота, чей id ЕСТЬ среди current_cards (остались невыполненными).
                Поле section берётся из АКТУАЛЬНОГО состояния (current_cards), не из снэпшота —
                чтобы учесть пересборки («пересобрать»), изменившие секцию в течение дня.
      - done:   карточки снэпшота, чьего id НЕТ среди current_cards, И для card_title которых
                НЕТ события с event_type in ('moved', 'overflow') в events.
                При наличии card_ctx: сначала проверяется реальное состояние через get_card
                (archive, регулярная в ожидаемой колонке, удалена → done; иначе → moved).
      - moved:  карточки снэпшота, чьего id НЕТ среди current_cards, И ЕСТЬ событие
                event_type in ('moved', 'overflow') с совпадающим card_title —
                detail берётся из ПОСЛЕДНЕГО такого события; либо карточки, обнаруженные
                через card_ctx как перенесённые вручную (detail='перенесено (обнаружено при сверке)').
      - added:  карточки current_cards, чьего id НЕТ среди id снэпшота (появились в течение дня)

    Если snapshot is None (утренняя логика сегодня не запускалась) — возвращает все
    current_cards как undone, done/moved/added — пустые списки.

    Возвращает:
        {
            "done":   [{"title": str, "importance": str|None, "size": int|None}, ...],
            "undone": [{"title": str, "importance": str|None, "size": int|None,
                        "section": str|None}, ...],
            "moved":  [{"title": str, "detail": str}, ...],
            "added":  [{"title": str, "importance": str|None, "size": int|None}, ...],
        }
    """
    # ── Снэпшот отсутствует — утро не запускалось ─────────────────────────────
    if snapshot is None:
        logger.info("diff_day: снэпшот отсутствует — все current_cards → undone")
        undone = [
            {
                "title":      c.get("title"),
                "importance": c.get("importance"),
                "size":       c.get("size"),
                "section":    c.get("section"),
            }
            for c in current_cards
        ]
        return {"done": [], "undone": undone, "moved": [], "added": []}

    # ── Множества id и словарь current_cards для эффективного поиска ──────────
    current_ids: set[int] = set()
    current_by_id: dict[int, dict] = {}   # id → актуальная карточка (для section)
    for c in current_cards:
        cid = c.get("id")
        if cid is not None:
            current_ids.add(cid)
            current_by_id[cid] = c

    snapshot_ids: set[int] = set()
    for c in snapshot:
        cid = c.get("id")
        if cid is not None:
            snapshot_ids.add(cid)

    # ── Индекс перемещений: title → detail последнего события moved/overflow ──
    # Итерируем в порядке id (хронологически), каждый следующий перезаписывает —
    # в результате остаётся только самый свежий detail для каждого названия.
    moved_detail: dict[str, str] = {}
    for event in events:
        if event.get("event_type") in ("moved", "overflow"):
            title = event.get("card_title", "")
            if title:
                moved_detail[title] = event.get("detail", "")

    # ── Классификация карточек снэпшота ──────────────────────────────────────
    done: list[dict] = []
    undone: list[dict] = []
    moved: list[dict] = []

    for card in snapshot:
        cid = card.get("id")
        if cid is None:
            logger.warning("diff_day: карточка в снэпшоте без поля id — пропускаем: {}", card)
            continue

        title = card.get("title")

        if cid in current_ids:
            # Карточка ещё в колонке дня → не выполнена.
            # ВАЖНО: section берём из ТЕКУЩЕГО состояния карточки, а не из снэпшота —
            # пересборка в течение дня могла изменить секцию (напр. День→Вечер).
            current = current_by_id.get(cid)
            undone.append({
                "title":      title,
                "importance": card.get("importance"),
                "size":       card.get("size"),
                "section":    current.get("section") if current else card.get("section"),
            })

        elif title in moved_detail:
            # Явное событие переноса/overflow → moved (detail из events)
            moved.append({
                "title":  title,
                "detail": moved_detail[title],
            })

        elif card_ctx is not None and cid in card_ctx.lookup:
            # Уточнённая классификация через предзагруженное реальное состояние карточки
            status, detail = _classify_disappeared(cid, card_ctx)
            if status == "done":
                done.append({
                    "title":      title,
                    "importance": card.get("importance"),
                    "size":       card.get("size"),
                })
            elif status == "moved":
                moved.append({
                    "title":  title,
                    "detail": detail or "",
                })
            else:
                # "undone" — крайний случай: карточка в той же колонке но не нашлась
                undone.append({
                    "title":      title,
                    "importance": card.get("importance"),
                    "size":       card.get("size"),
                    "section":    card.get("section"),   # fallback к снэпшоту
                })

        else:
            # Нет card_ctx (или карточка не в lookup) → старый дефолт: done
            done.append({
                "title":      title,
                "importance": card.get("importance"),
                "size":       card.get("size"),
            })

    # ── Карточки, добавленные в течение дня ──────────────────────────────────
    added: list[dict] = []
    for card in current_cards:
        cid = card.get("id")
        if cid is not None and cid not in snapshot_ids:
            added.append({
                "title":      card.get("title"),
                "importance": card.get("importance"),
                "size":       card.get("size"),
            })

    logger.info(
        "diff_day: done={} undone={} moved={} added={}",
        len(done), len(undone), len(moved), len(added),
    )
    return {"done": done, "undone": undone, "moved": moved, "added": added}
