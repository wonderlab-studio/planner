"""
evening_logic.py — вечерняя логика подведения итогов дня.

Класс EveningLogic реализует:
  - загрузку карточек сегодняшней колонки
  - разделение на выполненные / невыполненные
  - генерацию итога через ClaudeClient

Публичный метод:
    async def run(self, today: date) -> str
        Возвращает текст итога дня для отправки в Telegram.
"""

from __future__ import annotations

from datetime import date

from loguru import logger

from kaiten_client import Card, KaitenClient
from board_logic import BoardLogic, COLUMN_IDS, WEEKDAY_COLUMNS
from claude_client import ClaudeClient


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

        1. Загружает карточки сегодняшней колонки.
        2. Делит на выполненные (state == 3) и невыполненные (state != 3).
        3. Формирует списки dict для ClaudeClient.
        4. Возвращает текст итога.

        Не бросает исключения — при ошибках возвращает fallback-строку.
        """
        today_col_id = COLUMN_IDS[WEEKDAY_COLUMNS[today.weekday()]]
        logger.info("evening: загружаем карточки col={} ({})", today_col_id, today.isoformat())

        # ── 1. Загрузка ───────────────────────────────────────────────────────
        try:
            cards = await self._client.get_cards(today_col_id)
        except Exception as exc:
            logger.error("evening: не удалось загрузить карточки — {}", exc)
            return "⚠️ Не удалось загрузить задачи дня. Попробуй позже."

        # ── 2. Разделение ─────────────────────────────────────────────────────
        done:   list[Card] = []
        undone: list[Card] = []

        for card in cards:
            if card.blocked or card.archived:
                continue  # разделители и архивированные пропускаем
            if card.state == 3:
                done.append(card)
            else:
                undone.append(card)

        logger.info(
            "evening: выполнено={} не_выполнено={}", len(done), len(undone)
        )

        # ── 3. Форматируем в dict для ClaudeClient ────────────────────────────
        def _to_dict(card: Card) -> dict:
            return {
                "title":      card.title,
                "importance": card.importance,   # str | None
                "size":       card.size,          # int | None
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
