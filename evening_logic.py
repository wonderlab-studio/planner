"""
evening_logic.py — сравнение утреннего снэпшота с текущим состоянием дня.

Чистая функция без I/O: принимает снэпшот утра, лог событий дня и текущие карточки
сегодняшней колонки — возвращает 4 категории для вечернего итога (done/undone/moved/added).
Хранение снэпшота/лога — в db.py (SQLite). Оркестрация (загрузка из БД, Kaiten, Claude,
отправка в Telegram, очистка после отправки) — в scheduler.py.
"""

from __future__ import annotations

from loguru import logger


def diff_day(
    snapshot: list[dict] | None,
    events: list[dict],
    current_cards: list[dict],
) -> dict:
    """Сравнивает утренний снэпшот с текущим состоянием карточек сегодняшней колонки.

    snapshot       — из db.load_morning_snapshot: [{"id","title","size","importance","section"}, ...]
                     или None (если утренняя логика сегодня не запускалась)
    events         — из db.load_daily_events: [{"event_type","card_title","detail","created_at"}, ...]
    current_cards  — текущие карточки сегодняшней колонки: [{"id","title","size","importance",
                     "section",...}, ...] (лишние ключи в dict допустимы и игнорируются)

    Алгоритм (сопоставление по id между snapshot и current_cards; по card_title между
    snapshot и events):
      - undone: карточки снэпшота, чей id ЕСТЬ среди current_cards (остались невыполненными)
      - done:   карточки снэпшота, чьего id НЕТ среди current_cards, И для card_title которых
                НЕТ события с event_type in ('moved', 'overflow') в events
                (пропала из колонки без явного переноса → значит архивирована/выполнена)
      - moved:  карточки снэпшота, чьего id НЕТ среди current_cards, И ЕСТЬ событие
                event_type in ('moved', 'overflow') с совпадающим card_title —
                detail берётся из ПОСЛЕДНЕГО (самого свежего) такого события для этой карточки
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

    # ── Множества id для эффективного поиска ──────────────────────────────────
    current_ids: set[int] = set()
    for c in current_cards:
        cid = c.get("id")
        if cid is not None:
            current_ids.add(cid)

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
            # Карточка ещё в колонке дня → не выполнена
            undone.append({
                "title":      title,
                "importance": card.get("importance"),
                "size":       card.get("size"),
                "section":    card.get("section"),
            })
        elif title in moved_detail:
            # Ушла из колонки + есть событие переноса → moved
            moved.append({
                "title":  title,
                "detail": moved_detail[title],
            })
        else:
            # Ушла из колонки без явного переноса → выполнена/архивирована
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
