"""
evening_logic.py — вечерняя логика подведения итогов дня.

Класс EveningLogic реализует:
  - загрузку карточек сегодняшней колонки (невыполненные)
  - загрузку карточек Архива через /columns/{id}/cards (содержит column_changed_at)
  - фильтрацию выполненных сегодня по полю column_changed_at
  - генерацию итога через ClaudeClient

Публичный метод:
    async def run(self, today: date) -> str
        Возвращает текст итога дня для отправки в Telegram.

Логика определения «выполнено сегодня»:
    Kaiten возвращает поле column_changed_at при запросе через
    GET /columns/{column_id}/cards — дату и время попадания карточки
    в текущую колонку. Карточки Архива с column_changed_at == сегодня
    — это задачи, заархивированные сегодня (выполненные).
    Fallback: если column_changed_at отсутствует — карточка в done не попадает.
"""

from __future__ import annotations

from datetime import date, timezone, timedelta

from loguru import logger

from kaiten_client import Card, KaitenClient, ARCHIVE_COLUMN_ID
from board_logic import BoardLogic, COLUMN_IDS, WEEKDAY_COLUMNS
from claude_client import ClaudeClient

# UTC+3 для сравнения дат (карточки хранят время в МСК)
_TZ_MSK = timezone(timedelta(hours=3))


class EveningLogic:
    """Вечерняя логика: собирает статистику дня и генерирует итог через Claude."""

    def __init__(
        self,
        client: KaitenClient,
        logic: BoardLogic,
        claude: ClaudeClient,
    ) -> None:
        self._client = client
        self._logic = logic
        self._claude = claude

    # ── Точка входа ───────────────────────────────────────────────────────────

    async def run(self, today: date) -> str:
        """Подводит итог дня.

        1. Загружает карточки сегодняшней колонки → всё что осталось = невыполненные.
        2. Загружает Архив через /columns/{id}/cards → фильтрует по column_changed_at.
        3. Формирует списки dict для ClaudeClient.
        4. Возвращает текст итога.

        Не бросает исключения — при ошибках возвращает fallback-строку.
        """
        today_col_id = COLUMN_IDS[WEEKDAY_COLUMNS[today.weekday()]]
        logger.info("evening: загружаем карточки col={} ({})", today_col_id, today.isoformat())

        # ── 1. Невыполненные: карточки сегодняшней колонки ───────────────────
        # Всё что осталось в колонке дня к вечеру — не сделано.
        # Выполненные карточки уходят в Архив командой «готово»,
        # в колонке дня их уже нет.
        try:
            today_cards = await self._client.get_cards(today_col_id)
        except Exception as exc:
            logger.error("evening: не удалось загрузить карточки сегодня — {}", exc)
            return "⚠️ Не удалось загрузить задачи дня. Попробуй позже."

        undone: list[Card] = [
            c for c in today_cards
            if not c.blocked and not c.archived
        ]

        # ── 2. Выполненные: Архив, column_changed_at == сегодня ──────────────
        # Используем /columns/{id}/cards — единственный эндпоинт, который
        # возвращает column_changed_at (дата попадания в колонку).
        done: list[Card] = []
        try:
            archive_cards = await self._client.get_column_cards(ARCHIVE_COLUMN_ID)
            for card in archive_cards:
                if card.blocked:
                    continue
                changed = card.column_changed_at_parsed
                if changed is None:
                    continue
                # Переводим UTC → МСК для сравнения с локальной датой
                changed_local = changed.astimezone(_TZ_MSK)
                if changed_local.date() == today:
                    done.append(card)
            logger.info(
                "evening: архив загружен, выполнено сегодня={} (всего в архиве={})",
                len(done), len(archive_cards),
            )
        except Exception as exc:
            # Архив недоступен — продолжаем без выполненных, не падаем
            logger.warning("evening: не удалось загрузить Архив — {}", exc)

        logger.info(
            "evening: выполнено={} не_выполнено={}", len(done), len(undone)
        )

        # ── 3. Форматируем в dict для ClaudeClient ────────────────────────────
        def _to_dict(card: Card) -> dict:
            return {
                "title":      card.title,
                "importance": card.importance,  # str | None
                "size":       card.size,         # int | None
            }

        done_dicts   = [_to_dict(c) for c in done]
        undone_dicts = [_to_dict(c) for c in undone]

        # ── 4. Генерация итога через Claude ───────────────────────────────────
        try:
            summary = await self._claude.generate_evening_summary(done_dicts, undone_dicts)
        except Exception as exc:
            logger.error("evening: ошибка generate_evening_summary — {}", exc)
            return "⚠️ Не удалось сгенерировать итог дня. Попробуй позже."

        logger.info("evening: итог сгенерирован ({} символов)", len(summary))
        return summary
