"""
claude_client.py — интеграция с Anthropic Claude API.

Методы:
    generate_morning_plan  — красивый план дня для Telegram
    generate_evening_summary — итог дня + мотивация
    parse_intent           — парсинг команды пользователя в JSON
    search_card_by_title   — нечёткий поиск карточки по заголовку
    generate_card_advice   — совет Claude по конкретной задаче

Модель: claude-sonnet-4-6
Стек: anthropic SDK, loguru, python-dotenv

Экономия токенов:
  - Системные промпты кэшируются через prompt caching (cache_control ephemeral)
  - Минимум 1024 токенов для кэширования — промпты специально чуть развёрнуты
  - max_tokens=1000 (parse_intent — 256, достаточно для JSON)
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone, timedelta
from typing import Any

import anthropic
from dotenv import load_dotenv
from loguru import logger

from prompts import MORNING_SYSTEM, EVENING_SYSTEM, PARSE_INTENT_SYSTEM, SEARCH_SYSTEM, ADVICE_SYSTEM

load_dotenv()

# ── Конфиг ────────────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2500
MAX_TOKENS_INTENT = 256   # JSON-ответ короткий — экономим
MAX_TOKENS_SEARCH = 64    # {"id": 123} — минимум токенов
MAX_TOKENS_ADVICE = 600   # совет по карточке

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ── Форматтеры входных данных ─────────────────────────────────────────────────

def _format_card(card: dict) -> str:
    """Форматирует карточку в читаемую строку для промпта."""
    parts = [f"• {card.get('title', '(без названия)')}"]
    if card.get("importance"):
        parts.append(f"  важность: {card['importance']}")

    # Для секции «На контроле» — время не показываем (это ожидание, не запланированное событие)
    is_control = card.get("section") == "На контроле"

    if not is_control:
        segments = card.get("segments") or []
        if segments:
            segs_str = ", ".join(f"{s}–{e}" for s, e in segments)
            parts.append(f"  время: {segs_str}")
        elif card.get("event_time"):
            et = card["event_time"]  # "HH:MM"
            size = card.get("size")
            if size and size != 999:
                # Вычисляем конец из event_time + size (fallback когда segments не заданы)
                try:
                    h, m = int(et[:2]), int(et[3:5])
                    total = h * 60 + m + round(size * 60)
                    end_h, end_m = divmod(total, 60)
                    parts.append(f"  время: {et}–{end_h:02d}:{end_m:02d}")
                except Exception:
                    parts.append(f"  время: {et}")
            else:
                parts.append(f"  время: {et}")

    if card.get("due_date"):
        parts.append(f"  дедлайн: {card['due_date']}")
    if card.get("size") and card["size"] != 999:
        parts.append(f"  ~{card['size']} ч")
    if card.get("description"):
        desc = card["description"][:120]
        if len(card["description"]) > 120:
            desc += "…"
        parts.append(f"  {desc}")
    for comment in card.get("comments") or []:
        if comment:
            text = str(comment)[:100]
            if len(str(comment)) > 100:
                text += "…"
            parts.append(f"  💬 {text}")
    return "\n".join(parts)


def _format_cards_by_section(cards: list[dict]) -> str:
    """Группирует карточки по секциям и форматирует в текст."""
    sections: dict[str, list[dict]] = {
        "Утро": [], "День": [], "Вечер": [], "На контроле": [], "Прочее": []
    }
    for card in cards:
        section = card.get("section", "Прочее")
        if section not in sections:
            section = "Прочее"
        sections[section].append(card)

    lines: list[str] = []
    for section_name, section_cards in sections.items():
        if not section_cards:
            continue
        lines.append(f"\n[{section_name}]")
        for card in section_cards:
            lines.append(_format_card(card))

    return "\n".join(lines).strip() or "Задач нет."


def _format_card_simple(card: dict) -> str:
    """Упрощённый формат для вечернего итога."""
    title = card.get("title", "(без названия)")
    imp = card.get("importance", "")
    suffix = f" [{imp}]" if imp else ""
    return f"• {title}{suffix}"


# ── ClaudeClient ──────────────────────────────────────────────────────────────

class ClaudeClient:
    """Асинхронный клиент к Anthropic Claude API."""

    def __init__(self) -> None:
        if not _ANTHROPIC_KEY:
            logger.warning("ANTHROPIC_API_KEY не задан")
        self._client = anthropic.AsyncAnthropic(api_key=_ANTHROPIC_KEY)

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "ClaudeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── Утренний план ─────────────────────────────────────────────────────────

    async def generate_morning_plan(
        self,
        cards: list[dict],
        date_str: str,
    ) -> str:
        """Генерирует план дня для Telegram на основе списка карточек.

        Параметры:
            cards    — список карточек: [{title, description, importance, size,
                        due_date, event_time, segments, section}, ...]
            date_str — дата в формате "21 мая 2026, четверг"

        Возвращает:
            Текст плана в Markdown для отправки в Telegram.

        Пример промпта (user):
            "Дата: 21 мая 2026, четверг

            Задачи на день:

            [Утро]
            • Позвонить заказчику
              важность: критическое
              время: 09:00–10:00
              ~1 ч
            ..."
        """
        cards_text = _format_cards_by_section(cards)
        user_message = f"Дата: {date_str}\n\nЗадачи на день:\n{cards_text}"

        logger.debug("generate_morning_plan: {} карточек, дата={}", len(cards), date_str)

        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": MORNING_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            result = response.content[0].text
            logger.info(
                "generate_morning_plan: input={} cached_read={} output={}",
                response.usage.input_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0),
                response.usage.output_tokens,
            )
            return result
        except anthropic.APIError as e:
            logger.error("generate_morning_plan: API error — {}", e)
            return "⚠️ Не удалось сгенерировать план дня. Попробуй позже."

    # ── Вечерний итог ─────────────────────────────────────────────────────────

    async def generate_evening_summary(
        self,
        done: list[dict],
        undone: list[dict],
    ) -> str:
        """Генерирует вечерний итог дня для Telegram.

        Параметры:
            done   — выполненные карточки: [{title, importance, ...}, ...]
            undone — невыполненные карточки

        Возвращает:
            Текст итога в Markdown для отправки в Telegram.

        Пример промпта (user):
            "Сделано (3):
            • Позвонить заказчику [критическое]
            • Написать отчёт [важное]
            • Купить продукты

            Не сделано (2):
            • Разобрать почту [важное]
            • Спортзал"
        """
        done_lines = "\n".join(_format_card_simple(c) for c in done) or "ничего"
        undone_lines = "\n".join(_format_card_simple(c) for c in undone) or "ничего"

        user_message = (
            f"Сделано ({len(done)}):\n{done_lines}\n\n"
            f"Не сделано ({len(undone)}):\n{undone_lines}"
        )

        logger.debug(
            "generate_evening_summary: done={} undone={}", len(done), len(undone)
        )

        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": EVENING_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            result = response.content[0].text
            logger.info(
                "generate_evening_summary: input={} cached_read={} output={}",
                response.usage.input_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0),
                response.usage.output_tokens,
            )
            return result
        except anthropic.APIError as e:
            logger.error("generate_evening_summary: API error — {}", e)
            return "⚠️ Не удалось сгенерировать итог дня. Попробуй позже."

    # ── Парсинг намерений ─────────────────────────────────────────────────────

    async def parse_intent(self, user_text: str) -> dict:
        """Парсит текстовую команду пользователя в структуру действия.

        Параметры:
            user_text — команда: "создать Позвонить маме завтра утром важное"

        Возвращает:
            {
                "action":     "create" | "done" | "move" | "note",
                "title":      str | None,
                "column":     str | None,   # "Понедельник" / "Следующая неделя" / ...
                "section":    str | None,   # "Утро" / "День" / "Вечер" / "На контроле"
                "deadline":   str | None,   # "YYYY-MM-DD"
                "importance": str | None,   # "среднее" / "важное" / "критическое"
                "size":       float | None,  # часы, например 2 или 0.5
                "note":       str | None,
            }
            При ошибке парсинга возвращает {"action": "unknown", "raw": user_text}.

        Пример промпта (user):
            "создать Позвонить маме завтра утром важное"

        Пример ответа модели (только JSON, без пояснений):
            {"action": "create", "title": "Позвонить маме", "column": null,
             "section": "Утро", "deadline": "2026-05-22", "importance": "важное",
             "size": null, "note": null}
        """
        today_str = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
        system_prompt = PARSE_INTENT_SYSTEM.format(today=today_str)

        logger.debug("parse_intent: текст={!r}", user_text)

        raw_text = ""
        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_INTENT,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_text}],
            )
            raw_text = response.content[0].text.strip()
            logger.debug("parse_intent: raw_response={!r}", raw_text)

            # Убираем случайные markdown-обёртки если модель их добавила
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
                raw_text = raw_text.strip()

            result = json.loads(raw_text)
            logger.info("parse_intent: action={} title={!r}", result.get("action"), result.get("title"))
            return result

        except json.JSONDecodeError as e:
            logger.error("parse_intent: не удалось распарсить JSON — {} | raw={!r}", e, raw_text)
            return {"action": "unknown", "raw": user_text}
        except anthropic.APIError as e:
            logger.error("parse_intent: API error — {}", e)
            return {"action": "unknown", "raw": user_text}

    # ── Поиск карточки по заголовку ───────────────────────────────────────────

    async def search_card_by_title(
        self,
        query: str,
        cards: list[dict],
    ) -> dict | None:
        """Находит карточку, наиболее соответствующую поисковому запросу.

        Используется для команд «готово» и «перенести», когда пользователь
        пишет приблизительное название задачи.

        Параметры:
            query — поисковый запрос: "отчёт по проекту" / "позвонить маме"
            cards — список карточек: [{id, title, column}, ...]

        Возвращает:
            Карточку из переданного списка или None если ничего не подошло.

        Пример промпта (user):
            "Запрос: «отчёт»

            Карточки:
            1. id=101 | Написать отчёт по проекту | Среда
            2. id=102 | Позвонить заказчику | Четверг
            3. id=103 | Купить продукты | Пятница"

        Пример ответа модели:
            {"id": 101}
        """
        if not cards:
            return None

        # Форматируем список карточек для промпта
        lines = [f"Запрос: «{query}»\n\nКарточки:"]
        for i, card in enumerate(cards, start=1):
            col = card.get("column", "")
            col_suffix = f" | {col}" if col else ""
            lines.append(f"{i}. id={card['id']} | {card.get('title', '')} {col_suffix}".rstrip())
        user_message = "\n".join(lines)

        logger.debug("search_card_by_title: query={!r} cards={}", query, len(cards))

        raw_text = ""
        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_SEARCH,
                system=[
                    {
                        "type": "text",
                        "text": SEARCH_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            raw_text = response.content[0].text.strip()
            logger.debug("search_card_by_title: raw_response={!r}", raw_text)

            # Убираем случайные markdown-обёртки
            if raw_text.startswith("```"):
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
                raw_text = raw_text.strip()

            result = json.loads(raw_text)
            found_id = result.get("id")

            if found_id is None:
                logger.info("search_card_by_title: ничего не найдено для {!r}", query)
                return None

            # Находим карточку с таким id в переданном списке
            matched = next((c for c in cards if c["id"] == found_id), None)
            if matched is None:
                logger.warning(
                    "search_card_by_title: модель вернула id={} которого нет в списке", found_id
                )
                return None

            logger.info(
                "search_card_by_title: найдено id={} «{}»", found_id, matched.get("title")
            )
            return matched

        except json.JSONDecodeError as e:
            logger.error(
                "search_card_by_title: не удалось распарсить JSON — {} | raw={!r}", e, raw_text
            )
            return None
        except anthropic.APIError as e:
            logger.error("search_card_by_title: API error — {}", e)
            return None

    # ── Совет по карточке ─────────────────────────────────────────────────────

    async def generate_card_advice(
        self,
        question: str,
        card_title: str,
        description: str | None,
        comments: list[str],
    ) -> str:
        """Генерирует совет Claude по конкретной задаче из планировщика.

        Параметры:
            question   — вопрос пользователя по задаче
            card_title — название карточки
            description — описание карточки (может быть None)
            comments   — список текстов комментариев к карточке
                         (включая предыдущие вопросы/ответы если уже были)

        Возвращает:
            Текст совета в Markdown для отправки в Telegram.

        Пример промпта (user):
            "Вопрос: С чего начать?

            Задача: Написать отчёт по итогам квартала
            Описание: Нужно охватить продажи, маркетинг и производство
            Комментарии:
            💬 Согласовать структуру с Иваном до пятницы
            💬 Данные по продажам у Марины"
        """
        # Формируем контекст карточки
        context_parts = [f"Задача: {card_title}"]

        if description:
            context_parts.append(f"Описание: {description}")

        if comments:
            context_parts.append("Комментарии:")
            for comment in comments:
                if comment:
                    context_parts.append(f"💬 {comment}")

        card_context = "\n".join(context_parts)
        user_message = f"Вопрос: {question}\n\n{card_context}"

        logger.debug(
            "generate_card_advice: card={!r} comments={} question={!r}",
            card_title, len(comments), question,
        )

        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_ADVICE,
                system=[
                    {
                        "type": "text",
                        "text": ADVICE_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            result = response.content[0].text
            logger.info(
                "generate_card_advice: input={} cached_read={} output={}",
                response.usage.input_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0),
                response.usage.output_tokens,
            )
            return result
        except anthropic.APIError as e:
            logger.error("generate_card_advice: API error — {}", e)
            return "⚠️ Не удалось получить совет. Попробуй позже."
