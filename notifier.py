"""
notifier.py — модуль отправки уведомлений в Telegram без входящего апдейта.

Использует httpx напрямую (POST на api.telegram.org).
Конфиг из .env: TELEGRAM_TOKEN.
chat_id передаётся явно при создании экземпляра — поддержка мульти-пользователей.

Предназначен для вызова из scheduler.py и других модулей,
где нет входящего Update от пользователя.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── Конфиг ────────────────────────────────────────────────────────────────────

_TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")

_TELEGRAM_API_BASE = "https://api.telegram.org"
_SEND_MESSAGE_TIMEOUT = 15  # секунд

# Максимум символов в одном сообщении Telegram
_MAX_MESSAGE_LEN = 4096


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _split_text(text: str, max_len: int = _MAX_MESSAGE_LEN) -> list[str]:
    """Разбивает длинный текст на части не длиннее max_len.

    Старается разбить по переносам строк, а не по середине слова.
    """
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Ищем последний перенос строки в пределах лимита
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return parts


# ── Notifier ──────────────────────────────────────────────────────────────────

class Notifier:
    """Отправляет сообщения в Telegram-чат через Bot API без Update."""

    def __init__(self, chat_id: int) -> None:
        if not _TELEGRAM_TOKEN:
            logger.warning("Notifier: TELEGRAM_TOKEN не задан")

        self._chat_id = chat_id
        self._token = _TELEGRAM_TOKEN
        self._api_url = f"{_TELEGRAM_API_BASE}/bot{self._token}/sendMessage"

    # ── Внутренний хелпер ─────────────────────────────────────────────────────

    async def _post(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Отправляет одно сообщение. Возвращает True при успехе.

        При ошибке парсинга Markdown автоматически повторяет без форматирования.
        """
        payload: dict[str, Any] = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": parse_mode,
        }
        try:
            async with httpx.AsyncClient(timeout=_SEND_MESSAGE_TIMEOUT) as client:
                resp = await client.post(self._api_url, json=payload)

                if resp.status_code == 200:
                    return True

                # Telegram вернул ошибку — логируем детали
                body = resp.json() if resp.content else {}
                error_code    = body.get("error_code", resp.status_code)
                description   = body.get("description", "")

                # Если ошибка в разметке — повторяем без parse_mode
                if parse_mode != "text" and "can't parse" in description.lower():
                    logger.warning(
                        "Notifier._post: Markdown-ошибка ({}), повтор без форматирования",
                        description,
                    )
                    return await self._post(text, parse_mode="text")

                logger.error(
                    "Notifier._post: Telegram API error {} — {}",
                    error_code, description,
                )
                return False

        except httpx.TimeoutException:
            logger.error("Notifier._post: таймаут при отправке сообщения")
            return False
        except Exception as exc:
            logger.exception("Notifier._post: неожиданная ошибка — {}", exc)
            return False

    # ── Публичные методы ──────────────────────────────────────────────────────

    async def send(self, text: str) -> None:
        """Отправляет произвольный текст. Длинные сообщения разбивает на части."""
        if not text:
            logger.warning("Notifier.send: передан пустой текст, пропускаем")
            return

        parts = _split_text(text)
        for i, part in enumerate(parts, start=1):
            ok = await self._post(part)
            if ok:
                logger.info(
                    "Notifier.send: часть {}/{} отправлена ({} символов)",
                    i, len(parts), len(part),
                )
            else:
                logger.error(
                    "Notifier.send: не удалось отправить часть {}/{}", i, len(parts)
                )

    async def send_morning_plan(self, plan_text: str) -> None:
        """Отправляет утренний план дня.

        Добавляет заголовок если его нет (план от Claude уже содержит форматирование).
        """
        logger.info("Notifier.send_morning_plan: отправка плана ({} символов)", len(plan_text))
        await self.send(plan_text)

    async def send_evening_summary(self, summary_text: str) -> None:
        """Отправляет вечерний итог дня."""
        logger.info(
            "Notifier.send_evening_summary: отправка итога ({} символов)", len(summary_text)
        )
        await self.send(summary_text)

    async def send_reminder(
        self,
        card_title: str,
        minutes_left: int,
        important: bool,
    ) -> None:
        """Отправляет напоминание о предстоящей задаче.

        Параметры:
            card_title   — название карточки
            minutes_left — сколько минут до события (15 или 30)
            important    — True если важность «важное» или «критическое»

        Формат сообщения:
            ⚠️ ВАЖНО: через 30 мин → «Звонок с заказчиком»
            🔔 Напоминание: через 15 мин → «Купить продукты»
        """
        if important:
            prefix = f"⚠️ *ВАЖНО*: через {minutes_left} мин"
        else:
            prefix = f"🔔 Напоминание: через {minutes_left} мин"

        text = f"{prefix} → «{card_title}»"

        logger.info(
            "Notifier.send_reminder: «{}» через {} мин (important={})",
            card_title, minutes_left, important,
        )
        await self.send(text)
